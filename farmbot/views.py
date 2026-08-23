from __future__ import annotations

import discord
from datetime import datetime

from .calc import format_duration


def is_leader(member: discord.Member, leadership_role_ids: frozenset[int]) -> bool:
    return member.guild_permissions.administrator or any(r.id in leadership_role_ids for r in member.roles)


def has_farm_role(member: discord.Member, farm_role_id: int) -> bool:
    return member.guild_permissions.administrator or any(r.id == farm_role_id for r in member.roles)



class PendingAmountModal(discord.ui.Modal, title="Назначить сумму"):
    amount = discord.ui.TextInput(
        label="Сумма",
        placeholder="Например: 250000",
        min_length=1,
        max_length=12,
    )

    def __init__(self, bot, user_id: int, day: str, source_channel_id: int, source_message_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = int(user_id)
        self.day = day
        self.source_channel_id = int(source_channel_id)
        self.source_message_id = int(source_message_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message(
                "⛔ Недостаточно прав.", ephemeral=True
            )

        try:
            raw = str(self.amount).replace(" ", "").replace("_", "")
            value = int(raw)
            if value < 0:
                raise ValueError("Сумма не может быть отрицательной.")

            now = datetime.now(self.bot.config.timezone)
            await self.bot.db.assign_manual_amount(
                self.user_id, self.day, value, interaction.user.id, now
            )
            await self.bot.db.audit(
                now,
                "manual_amount",
                interaction.user.id,
                self.user_id,
                f"{self.day}={value}",
            )

            text = (
                f"✅ Для <@{self.user_id}> на **{self.day}** назначено "
                f"**{value:,}$**."
            ).replace(",", " ")
            await interaction.response.send_message(text, ephemeral=True)

            try:
                channel = self.bot.get_channel(self.source_channel_id)
                if isinstance(channel, discord.TextChannel):
                    message = await channel.fetch_message(self.source_message_id)
                    embed = message.embeds[0] if message.embeds else None
                    if embed:
                        embed = discord.Embed.from_dict(embed.to_dict())
                        embed.add_field(
                            name="Решение руководства",
                            value=f"✅ Назначено: **{value:,}$**".replace(",", " "),
                            inline=False,
                        )
                    await message.edit(
                        embed=embed,
                        view=AssignedAmountView(self.bot, self.user_id, self.day, value)
                    )
            except discord.HTTPException:
                pass

        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)


class PendingAmountView(discord.ui.View):
    def __init__(self, bot, user_id: int, day: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = int(user_id)
        self.day = day

        button = discord.ui.Button(
            label="Назначить сумму",
            emoji="💵",
            style=discord.ButtonStyle.primary,
            custom_id=f"daily_amount:{self.user_id}:{self.day.replace('-', '')}",
        )
        button.callback = self._open_modal
        self.add_item(button)

    async def _open_modal(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message(
                "⛔ Недостаточно прав.", ephemeral=True
            )

        if interaction.message is None or interaction.channel_id is None:
            return await interaction.response.send_message("❌ Не удалось определить карточку отчёта.", ephemeral=True)
        await interaction.response.send_modal(
            PendingAmountModal(
                self.bot,
                self.user_id,
                self.day,
                interaction.channel_id,
                interaction.message.id
            )
        )


class AssignedAmountView(discord.ui.View):
    """Review the day's manual amount after another sub-threshold session."""
    def __init__(self, bot, user_id: int, day: str, amount: int):
        super().__init__(timeout=None)
        self.bot, self.user_id, self.day, self.amount = bot, int(user_id), day, int(amount)
        keep = discord.ui.Button(label=f"Оставить {amount:,}$".replace(",", " "), emoji="✅", style=discord.ButtonStyle.success, custom_id=f"daily_keep:{user_id}:{day.replace('-', '')}")
        change = discord.ui.Button(label="Изменить сумму", emoji="✏️", style=discord.ButtonStyle.primary, custom_id=f"daily_change:{user_id}:{day.replace('-', '')}")
        keep.callback, change.callback = self._keep, self._change
        self.add_item(keep); self.add_item(change)

    async def _keep(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message(
                "⛔ Недостаточно прав.", ephemeral=True
            )

        async with self.bot.db.connect() as db:
            row = await (await db.execute("""
                SELECT manual_amount FROM daily_reports
                WHERE user_id=? AND day=?
            """, (self.user_id, self.day))).fetchone()

        current = (
            int(row["manual_amount"])
            if row and row["manual_amount"] is not None
            else self.amount
        )
        await self.bot.db.audit(
            datetime.now(self.bot.config.timezone),
            "manual_amount_keep",
            interaction.user.id,
            self.user_id,
            f"{self.day}={current}"
        )

        for child in self.children:
            child.disabled = True

        await interaction.response.send_message(
            f"✅ Сумма за {self.day} оставлена: **{current:,}$**.".replace(",", " "),
            ephemeral=True
        )
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _change(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(interaction.user, self.bot.config.leadership_role_ids):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        if interaction.message is None or interaction.channel_id is None:
            return await interaction.response.send_message("❌ Не удалось определить карточку.", ephemeral=True)
        await interaction.response.send_modal(PendingAmountModal(self.bot, self.user_id, self.day, interaction.channel_id, interaction.message.id))


class SettingModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Новое значение", placeholder="50000")

    def __init__(self, bot, key: str, title: str):
        super().__init__(title=title, timeout=300)
        self.bot = bot
        self.key = key

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(interaction.user, self.bot.config.leadership_role_ids):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        try:
            val = int(str(self.value).replace(" ", ""))
            if val < 0:
                raise ValueError("Значение не может быть отрицательным.")
            await self.bot.db.set_setting(self.key, val)
            await self.bot.db.audit(datetime.now(self.bot.config.timezone), "setting", interaction.user.id, details=f"{self.key}={val}")
            await interaction.response.send_message(f"✅ Сохранено: **{val:,}**".replace(",", " "), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)


class MainPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def guard(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not has_farm_role(interaction.user, self.bot.config.farm_role_id):
            await interaction.response.send_message("⛔ Эта панель только для отдела FARM.", ephemeral=True)
            return False
        now = datetime.now(self.bot.config.timezone)
        await self.bot.db.ensure_member(interaction.user.id, interaction.user.display_name, now)
        return True

    @discord.ui.button(label="Начать фарм", emoji="🟢", style=discord.ButtonStyle.success, custom_id="farm:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        try:
            now = datetime.now(self.bot.config.timezone)
            await self.bot.db.start_farm(interaction.user.id, now)
            await self.bot.db.audit(now, "farm_start", interaction.user.id, interaction.user.id)
            await interaction.response.send_message("🌾 Фарм начат. Таймер пошёл.", ephemeral=True)
            await self.bot.refresh_panels()
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @discord.ui.button(label="Завершить фарм", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="farm:finish")
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            now = datetime.now(self.bot.config.timezone)
            result = await self.bot.db.finish_farm(
                interaction.user.id, now, self.bot.config.timezone
            )
            await self.bot.db.audit(
                now, "farm_finish",
                interaction.user.id, interaction.user.id, str(result)
            )

            report_ok = True
            report_error = None

            if result["status"] == "auto":
                text = (
                    f"✅ Фарм завершён.\n"
                    f"Рабочий день: **{result['work_day']}**\n"
                    f"Суммарно: **{format_duration(result['seconds'])}**\n"
                    f"Дневное начисление: **{result['amount']:,}$** автоматически."
                ).replace(",", " ")

            elif result["status"] == "manual":
                try:
                    await self.bot.notify_pending_amount(
                        interaction.user, result["work_day"], result["seconds"]
                    )
                except Exception as exc:
                    report_ok = False
                    report_error = str(exc)

                text = (
                    f"✅ Фарм завершён.\n"
                    f"Рабочий день: **{result['work_day']}**\n"
                    f"Суммарно: **{format_duration(result['seconds'])}**\n"
                    f"Текущая назначенная сумма: **{result['amount']:,}$**."
                ).replace(",", " ")

            else:
                try:
                    await self.bot.notify_pending_amount(
                        interaction.user, result["work_day"], result["seconds"]
                    )
                except Exception as exc:
                    report_ok = False
                    report_error = str(exc)

                text = (
                    f"✅ Фарм завершён.\n"
                    f"Рабочий день: **{result['work_day']}**\n"
                    f"Суммарно: **{format_duration(result['seconds'])}**\n"
                    f"⏳ Меньше порога — руководство должно назначить сумму."
                )

            if result["status"] in ("pending", "manual"):
                if report_ok:
                    text += "\n📨 **Новый отчёт отправлен руководству.**"
                else:
                    text += (
                        "\n⚠️ **Смена сохранена, но отчёт не доставлен.**\n"
                        f"Причина: `{report_error}`"
                    )

            await interaction.followup.send(text, ephemeral=True)
            await self.bot.refresh_panels()

        except Exception as exc:
            await interaction.followup.send(
                f"❌ Ошибка при завершении фарма: `{exc}`",
                ephemeral=True
            )

    @discord.ui.button(label="Уехать на контент", emoji="🎯", style=discord.ButtonStyle.primary, custom_id="farm:content")
    async def content(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        try:
            now = datetime.now(self.bot.config.timezone)
            await self.bot.db.pause_for_content(interaction.user.id, now)
            await self.bot.db.audit(now, "content_start", interaction.user.id, interaction.user.id)
            await interaction.response.send_message(
                "🎯 Вы отмечены как «На контенте».\nЕсли вы фармили — фарм-таймер поставлен на паузу.",
                ephemeral=True)
            await self.bot.refresh_panels()
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @discord.ui.button(label="Вернуться с контента", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="farm:return")
    async def return_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        try:
            now = datetime.now(self.bot.config.timezone)
            cid = await self.bot.db.return_from_content(interaction.user.id, now)
            await self.bot.db.audit(now, "content_return", interaction.user.id, interaction.user.id, f"content_id={cid}")
            ch = self.bot.get_channel(self.bot.config.content_proof_channel_id)
            await interaction.response.send_message(
                f"↩️ Контент завершён. Если до него вы фармили — таймер снова идёт.\n"
                f"📸 **Теперь обязательно отправьте фото участия** в {ch.mention if ch else 'канал подтверждений'}.\n"
                f"Без фото контент не даст скидку.",
                ephemeral=True)
            await self.bot.refresh_panels()
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    @discord.ui.button(label="Мой день", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="farm:day")
    async def my_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.my_day_embed(interaction.user.id, interaction.user.display_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Моя статистика", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="farm:me")
    async def me(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.member_stats_embed(interaction.user.id, interaction.user.display_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Внести недельный взнос", emoji="💰", style=discord.ButtonStyle.secondary, custom_id="farm:pay")
    async def pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        now = datetime.now(self.bot.config.timezone)
        summary = await self.bot.db.member_week_summary(
            interaction.user.id, now.date(), self.bot.config.timezone, now
        )
        if summary["due"] is None:
            return await interaction.response.send_message(
                "⏳ Итоговый взнос ещё нельзя внести: есть дни, где руководство не назначило сумму.",
                ephemeral=True
            )
        status = await self.bot.db.payment_status(interaction.user.id, summary["week_start"].isoformat())
        if status == "paid":
            return await interaction.response.send_message("✅ Взнос за эту неделю уже подтверждён.", ephemeral=True)
        self.bot.pending_payment_uploads[interaction.user.id] = summary["week_start"].isoformat()
        ch = self.bot.get_channel(self.bot.config.payment_proof_channel_id)
        await interaction.response.send_message(
            f"💰 К оплате: **{summary['due']:,}$**\n"
            f"📸 Отправьте скрин внесения в {ch.mention if ch else 'канал подтверждений взносов'}. "
            f"Бот возьмёт следующее изображение как отчёт.".replace(",", " "),
            ephemeral=True
        )


class AdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def guard(self, interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(interaction.user, self.bot.config.leadership_role_ids):
            await interaction.response.send_message("⛔ Панель доступна только руководству.", ephemeral=True)
            return False
        return True


    @discord.ui.button(label="Ожидают сумму", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="admin:pending")
    async def pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        rows = await self.bot.db.pending_daily_reports()
        if not rows:
            return await interaction.response.send_message("✅ Нет отчётов без назначенной суммы.", ephemeral=True)
        text = "\n".join(f"• <@{r['user_id']}> — **{r['day']}**" for r in rows[:25])
        await interaction.response.send_message("⏳ **Ожидают сумму**\n" + text, ephemeral=True)

    @discord.ui.button(label="Статистика отдела", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="admin:stats")
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.department_stats_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Карточка игрока", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="admin:player")
    async def player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.send_message(
            "Выберите участника FARM:",
            view=PlayerPickerView(self.bot),
            ephemeral=True
        )

    @discord.ui.button(label="Оплаты", emoji="💰", style=discord.ButtonStyle.secondary, custom_id="admin:payments")
    async def payments(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.send_message(
            "Выберите категорию:",
            view=PaymentControlView(self.bot),
            ephemeral=True
        )

    @discord.ui.button(label="Сравнение недель", emoji="📈", style=discord.ButtonStyle.secondary, custom_id="admin:compare")
    async def compare(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.week_comparison_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Недельный отчёт", emoji="🧾", style=discord.ButtonStyle.secondary, custom_id="admin:weekly_report")
    async def weekly_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        await interaction.response.send_message(
            "Настройка автоматического недельного отчёта:",
            view=WeeklyReportSettingsView(self.bot),
            ephemeral=True
        )

    @discord.ui.button(label="Проверить отчёты", emoji="🧪", style=discord.ButtonStyle.secondary, custom_id="admin:test_reports")
    async def test_reports(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.bot.resolve_text_channel(
                self.bot.config.report_channel_id, "канал FARM-отчётов"
            )
            message = await channel.send(
                "🧪 **Проверка FARM-отчётов**\n"
                f"Запустил: {interaction.user.mention}\n"
                "Если это сообщение видно — REPORT_CHANNEL_ID и права бота работают."
            )
            await interaction.followup.send(
                f"✅ Тест отправлен в {channel.mention}. Message ID: `{message.id}`",
                ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Тест отчётов не прошёл: `{exc}`",
                ephemeral=True
            )

    @discord.ui.button(label="Настройки", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="admin:settings")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.guard(interaction): return
        view = MoneySettingsView(self.bot)
        auto = await self.bot.db.setting_int("auto_daily_amount")
        disc = await self.bot.db.setting_int("content_discount")
        threshold = await self.bot.db.setting_int("farm_threshold_minutes")
        minimum = await self.bot.db.setting_int("minimum_weekly_due")
        reminder = await self.bot.db.setting_int("session_reminder_minutes")
        msg = (
            f"⚙️ **Настройки расчёта**\n"
            f"Порог фарма: **{threshold//60}ч {threshold%60}м**\n"
            f"Автосумма при достижении порога: **{auto:,}$**\n"
            f"Скидка за подтверждённый контент: **{disc:,}$**\n"
            f"Минимальный недельный взнос: **{minimum:,}$**\n"
            f"Напоминание о непрерывном фарме: **{reminder} мин.**"
        ).replace(",", " ")
        await interaction.response.send_message(msg, view=view, ephemeral=True)


class MoneySettingsView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Порог фарма", style=discord.ButtonStyle.secondary)
    async def threshold(self, interaction, button):
        await interaction.response.send_modal(SettingModal(self.bot, "farm_threshold_minutes", "Порог фарма в минутах"))

    @discord.ui.button(label="Сумма 4+ часов", style=discord.ButtonStyle.secondary)
    async def auto_amount(self, interaction, button):
        await interaction.response.send_modal(SettingModal(self.bot, "auto_daily_amount", "Автоматическая дневная сумма"))

    @discord.ui.button(label="Скидка за контент", style=discord.ButtonStyle.secondary)
    async def discount(self, interaction, button):
        await interaction.response.send_modal(SettingModal(self.bot, "content_discount", "Скидка за 1 контент"))

    @discord.ui.button(label="Мин. взнос недели", style=discord.ButtonStyle.secondary)
    async def min_due(self, interaction, button):
        await interaction.response.send_modal(SettingModal(self.bot, "minimum_weekly_due", "Минимальный недельный взнос"))

    @discord.ui.button(label="Напоминание сессии", style=discord.ButtonStyle.secondary)
    async def session_reminder(self, interaction, button):
        await interaction.response.send_modal(
            SettingModal(self.bot, "session_reminder_minutes", "Напоминание о сессии, минут")
        )



class ContentReviewView(discord.ui.View):
    def __init__(self, bot, content_id: int, user_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.content_id = int(content_id)
        self.user_id = int(user_id)

        approve = discord.ui.Button(
            label="Подтвердить", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"content:approve:{self.content_id}"
        )
        reject = discord.ui.Button(
            label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger,
            custom_id=f"content:reject:{self.content_id}"
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    async def _review(self, interaction, approve: bool):
        if not isinstance(interaction.user, discord.Member) or not is_leader(interaction.user, self.bot.config.leadership_role_ids):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        try:
            now = datetime.now(self.bot.config.timezone)
            await self.bot.db.review_content(self.content_id, approve, interaction.user.id, now)
            await self.bot.db.audit(now, "content_approve" if approve else "content_reject",
                                    interaction.user.id, self.user_id, f"content_id={self.content_id}")
            await interaction.response.send_message("✅ Контент подтверждён." if approve else "❌ Контент отклонён.", ephemeral=True)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    async def _approve(self, interaction):
        await self._review(interaction, True)

    async def _reject(self, interaction):
        await self._review(interaction, False)


class PaymentReviewView(discord.ui.View):
    def __init__(self, bot, user_id: int, week_start: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = int(user_id)
        self.week_start = week_start

        key = week_start.replace("-", "")
        approve = discord.ui.Button(
            label="Подтвердить взнос", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"payment:approve:{self.user_id}:{key}"
        )
        reject = discord.ui.Button(
            label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger,
            custom_id=f"payment:reject:{self.user_id}:{key}"
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    async def _review(self, interaction, approve: bool):
        if not isinstance(interaction.user, discord.Member) or not is_leader(interaction.user, self.bot.config.leadership_role_ids):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        try:
            now = datetime.now(self.bot.config.timezone)
            await self.bot.db.review_payment(self.user_id, self.week_start, approve, interaction.user.id, now)
            await self.bot.db.audit(now, "payment_approve" if approve else "payment_reject",
                                    interaction.user.id, self.user_id, f"week={self.week_start}")
            await interaction.response.send_message("✅ Взнос подтверждён." if approve else "❌ Взнос отклонён.", ephemeral=True)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

    async def _approve(self, interaction):
        await self._review(interaction, True)

    async def _reject(self, interaction):
        await self._review(interaction, False)


class PlayerUserSelect(discord.ui.UserSelect):
    def __init__(self, bot):
        super().__init__(
            placeholder="Выберите игрока",
            min_values=1,
            max_values=1,
            custom_id="admin:player_select"
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)

        selected = self.values[0]
        guild = interaction.guild
        member = guild.get_member(selected.id) if guild else None
        if member is None:
            return await interaction.response.send_message("❌ Участник не найден на сервере.", ephemeral=True)
        if not has_farm_role(member, self.bot.config.farm_role_id):
            return await interaction.response.send_message("❌ У выбранного пользователя нет роли FARM.", ephemeral=True)

        now = datetime.now(self.bot.config.timezone)
        await self.bot.db.ensure_member(member.id, member.display_name, now)
        embed = await self.bot.player_card_embed(member.id, member.display_name)
        await interaction.response.edit_message(content=None, embed=embed, view=PlayerPickerView(self.bot))


class PlayerPickerView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot
        self.add_item(PlayerUserSelect(bot))


class PaymentControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def _show(self, interaction: discord.Interaction, category: str, title: str):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.payment_category_embed(category, title)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Оплатили", emoji="✅", style=discord.ButtonStyle.success)
    async def paid(self, interaction, button):
        await self._show(interaction, "paid", "✅ Оплатили")

    @discord.ui.button(label="На проверке", emoji="⏳", style=discord.ButtonStyle.primary)
    async def pending(self, interaction, button):
        await self._show(interaction, "pending", "⏳ На проверке")

    @discord.ui.button(label="Не оплатили", emoji="❌", style=discord.ButtonStyle.danger)
    async def unpaid(self, interaction, button):
        await self._show(interaction, "unpaid", "❌ Не оплатили")

    @discord.ui.button(label="Не рассчитано", emoji="⚠️", style=discord.ButtonStyle.secondary)
    async def unresolved(self, interaction, button):
        await self._show(interaction, "unresolved", "⚠️ Взнос не рассчитан")


class WeeklyReportScheduleModal(discord.ui.Modal, title="Расписание недельного отчёта"):
    weekday = discord.ui.TextInput(
        label="День недели (1=Пн ... 7=Вс)",
        placeholder="7",
        min_length=1,
        max_length=1
    )
    report_time = discord.ui.TextInput(
        label="Время по Москве, ЧЧ:ММ",
        placeholder="23:00",
        min_length=5,
        max_length=5
    )

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        try:
            weekday = int(str(self.weekday))
            if weekday < 1 or weekday > 7:
                raise ValueError("День недели должен быть от 1 до 7.")
            raw = str(self.report_time).strip()
            parts = raw.split(":")
            if len(parts) != 2:
                raise ValueError("Время укажите в формате ЧЧ:ММ.")
            hour, minute = map(int, parts)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Некорректное время.")

            await self.bot.db.set_setting("weekly_report_weekday", weekday - 1)
            await self.bot.db.set_setting("weekly_report_hour", hour)
            await self.bot.db.set_setting("weekly_report_minute", minute)
            await interaction.response.send_message(
                f"✅ Отчёт: день **{weekday}**, время **{hour:02d}:{minute:02d}** ({self.bot.config.timezone_name}).",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)


class WeeklyReportSettingsView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="Изменить расписание", emoji="🕒", style=discord.ButtonStyle.primary)
    async def schedule(self, interaction, button):
        await interaction.response.send_modal(WeeklyReportScheduleModal(self.bot))

    @discord.ui.button(label="Вкл/выкл автоотчёт", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction, button):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        current = await self.bot.db.setting_int("weekly_report_enabled")
        new = 0 if current else 1
        await self.bot.db.set_setting("weekly_report_enabled", new)
        await interaction.response.send_message(
            "✅ Автоотчёт включён." if new else "⏸️ Автоотчёт выключен.",
            ephemeral=True
        )

    @discord.ui.button(label="Показать сейчас", emoji="🧾", style=discord.ButtonStyle.secondary)
    async def preview(self, interaction, button):
        if not isinstance(interaction.user, discord.Member) or not is_leader(
            interaction.user, self.bot.config.leadership_role_ids
        ):
            return await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.weekly_report_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)


class SessionReminderView(discord.ui.View):
    def __init__(self, bot, user_id: int, segment_started_at: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = int(user_id)
        self.segment_started_at = segment_started_at
        safe = segment_started_at.replace("-", "").replace(":", "").replace("+", "p").replace(".", "")
        safe = safe[-50:]

        cont = discord.ui.Button(
            label="Да, продолжаю",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"session:continue:{self.user_id}:{safe}"
        )
        stop = discord.ui.Button(
            label="Завершить фарм",
            emoji="🛑",
            style=discord.ButtonStyle.danger,
            custom_id=f"session:finish:{self.user_id}:{safe}"
        )
        cont.callback = self._continue
        stop.callback = self._finish
        self.add_item(cont)
        self.add_item(stop)

    async def _guard(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⛔ Это напоминание предназначено другому игроку.", ephemeral=True)
            return False
        return True

    async def _continue(self, interaction):
        if not await self._guard(interaction): return
        now = datetime.now(self.bot.config.timezone)
        state = await self.bot.db.get_state(self.user_id)
        if state.get("state") != "farming" or state.get("farming_started_at") != self.segment_started_at:
            await self.bot.db.resolve_session_reminder(
                self.user_id, self.segment_started_at, "continue", now
            )
            return await interaction.response.send_message("ℹ️ Эта фарм-сессия уже не активна.", ephemeral=True)

        await self.bot.db.resolve_session_reminder(
            self.user_id, self.segment_started_at, "continue", now
        )
        for child in self.children:
            child.disabled = True
        await interaction.response.send_message("✅ Хорошо, фарм продолжается.", ephemeral=True)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def _finish(self, interaction):
        if not await self._guard(interaction): return
        now = datetime.now(self.bot.config.timezone)
        try:
            result = await self.bot.db.finish_farm(self.user_id, now, self.bot.config.timezone)
            await self.bot.db.resolve_session_reminder(
                self.user_id, self.segment_started_at, "finish", now
            )
            await self.bot.db.audit(now, "farm_finish_reminder", self.user_id, self.user_id, str(result))
            text = f"🛑 Фарм завершён. Сегодня: **{format_duration(result['seconds'])}**."
            if result["status"] == "auto":
                text += f"\nНачисление: **{result['amount']:,}$**.".replace(",", " ")
            elif result["status"] == "manual":
                text += f"\nТекущая сумма: **{result['amount']:,}$**. Руководству отправлено обновление.".replace(",", " ")
                guild = self.bot.get_guild(self.bot.config.guild_id)
                member = guild.get_member(self.user_id) if guild else None
                if member:
                    await self.bot.notify_pending_amount(member, result["work_day"], result["seconds"])
            elif result["status"] == "pending":
                text += "\n⏳ Руководство назначит персональную сумму."
                guild = self.bot.get_guild(self.bot.config.guild_id)
                member = guild.get_member(self.user_id) if guild else None
                if member:
                    await self.bot.notify_pending_amount(member, result["work_day"], result["seconds"])
            for child in self.children:
                child.disabled = True
            await interaction.response.send_message(text, ephemeral=True)
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await self.bot.refresh_panels()
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
