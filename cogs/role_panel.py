import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
from utils import roles as role_utils
from utils.nsfw_check import is_image_nsfw


class RoleEditModal(discord.ui.Modal, title="تعديل الرتبة الخاصة"):
    name = discord.ui.TextInput(label="الاسم الجديد", required=False, max_length=100)

    def __init__(self, bot, role_id: int):
        super().__init__()
        self.bot = bot
        self.role_id = role_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        success, msg = await role_utils.do_edit_role(
            self.bot, interaction.guild, interaction.user, self.role_id,
            name=self.name.value or None,
        )
        await interaction.followup.send(msg, ephemeral=True)


class RemoveMemberSelect(discord.ui.Select):
    def __init__(self, bot, role_id: int, options: list[discord.SelectOption]):
        self.bot = bot
        self.role_id = role_id
        super().__init__(placeholder="اختر العضو الذي تريد سحب رتبتك منه", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        target_id = int(self.values[0])
        success, msg = await role_utils.do_remove_shared_member(
            self.bot, interaction.guild, interaction.user, self.role_id, target_id
        )
        await interaction.followup.send(msg, ephemeral=True)


class AddMemberSelect(discord.ui.UserSelect):
    def __init__(self, bot, role_id: int):
        self.bot = bot
        self.role_id = role_id
        super().__init__(placeholder="اختر العضو الذي تريد إضافته إلى رتبتك الخاصة", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        target = self.values[0]
        if isinstance(target, discord.User):
            try:
                target = await interaction.guild.fetch_member(target.id)
            except Exception:
                await interaction.followup.send("تعذر إيجاد هذا العضو في السيرفر.", ephemeral=True)
                return
        success, msg = await role_utils.do_add_shared_member(
            self.bot, interaction.guild, interaction.user, self.role_id, target
        )
        await interaction.followup.send(msg, ephemeral=True)


class RoleChooserSelect(discord.ui.Select):
    """قائمة اختيار تظهر فقط عند امتلاك المستخدم أكثر من رتبة اشتراك فعالة،
    حتى يحدد الرتبة التي يقصدها قبل تنفيذ الإجراء (إضافة/إزالة عضو، تعديل...)."""

    def __init__(self, guild: discord.Guild, role_ids: list[int], on_select):
        self.on_select = on_select
        options = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            label = role.name if role else f"رتبة محذوفة ({role_id})"
            options.append(discord.SelectOption(label=label, value=str(role_id)))
        super().__init__(placeholder="اختر الرتبة التي تريد المتابعة بها", options=options)

    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        await self.on_select(interaction, role_id)


class RoleChooserView(discord.ui.View):
    def __init__(self, guild: discord.Guild, role_ids: list[int], on_select):
        super().__init__(timeout=120)
        self.add_item(RoleChooserSelect(guild, role_ids, on_select))


async def _resolve_role_or_prompt(interaction: discord.Interaction, on_resolved, prompt: str = None):
    """يجلب رتب اشتراك المستخدم الفعالة:
    - لا يوجد رتبة: يرسل رسالة الخطأ المعتادة.
    - رتبة واحدة: ينفّذ on_resolved(interaction, role_id) مباشرة (نفس السلوك القديم بالضبط).
    - أكثر من رتبة: يعرض قائمة اختيار، وعند الاختيار ينفّذ on_resolved(interaction, role_id).
    """
    role_ids = await asyncio.to_thread(db.get_owner_role_ids, interaction.guild.id, interaction.user.id)
    if not role_ids:
        await interaction.response.send_message(
            "لا يوجد لديك أي اشتراك فعال حالياً. هذه الميزة مخصصة للمشتركين فقط.", ephemeral=True
        )
        return
    if len(role_ids) == 1:
        await on_resolved(interaction, role_ids[0])
        return

    await interaction.response.send_message(
        prompt or "لديك أكثر من رتبة اشتراك فعالة في نفس الوقت. اختر الرتبة التي تريد المتابعة بها:",
        view=RoleChooserView(interaction.guild, role_ids, on_resolved),
        ephemeral=True
    )


class RolePanelView(discord.ui.View):
    """View دائم (timeout=None) يُسجَّل مرة واحدة بـ setup_hook حتى تبقى الأزرار
    تعمل بعد إعادة تشغيل البوت."""

    def __init__(self, bot=None):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="إضافة عضو", style=discord.ButtonStyle.gray, custom_id="role_panel_add")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._add_member_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد إضافة عضو إليها:"
        )

    async def _add_member_action(self, interaction: discord.Interaction, role_id: int):
        view = discord.ui.View(timeout=120)
        view.add_item(AddMemberSelect(self.bot, role_id))
        await interaction.response.send_message("اختر العضو الذي تريد إضافته إلى رتبتك الخاصة:", view=view, ephemeral=True)

    @discord.ui.button(label="إزالة عضو", style=discord.ButtonStyle.gray, custom_id="role_panel_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._remove_member_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد إزالة عضو منها:"
        )

    async def _remove_member_action(self, interaction: discord.Interaction, role_id: int):
        shared_ids = await asyncio.to_thread(db.get_shared_members, interaction.user.id, interaction.guild.id, role_id)
        if not shared_ids:
            await interaction.response.send_message("لم تقم بإضافة أي عضو حتى الآن إلى رتبتك الخاصة", ephemeral=True)
            return

        options = []
        for member_id in shared_ids:
            member = interaction.guild.get_member(member_id)
            label = member.display_name if member else f"عضو مغادر ({member_id})"
            options.append(discord.SelectOption(label=label, value=str(member_id)))

        view = discord.ui.View(timeout=120)
        view.add_item(RemoveMemberSelect(self.bot, role_id, options))
        await interaction.response.send_message("اختر العضو الذي تريد سحب رتبتك منه:", view=view, ephemeral=True)

    @discord.ui.button(label="تعديل الرتبة", style=discord.ButtonStyle.gray, custom_id="role_panel_edit")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._edit_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد تعديلها:"
        )

    async def _edit_action(self, interaction: discord.Interaction, role_id: int):
        await interaction.response.send_modal(RoleEditModal(self.bot, role_id))

    @discord.ui.button(label="إزالة الأيقونة", style=discord.ButtonStyle.gray, custom_id="role_panel_remove_icon")
    async def remove_icon_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._remove_icon_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد إزالة أيقونتها:"
        )

    async def _remove_icon_action(self, interaction: discord.Interaction, role_id: int):
        await interaction.response.defer(ephemeral=True)
        success, msg = await role_utils.do_remove_role_icon(self.bot, interaction.guild, interaction.user, role_id)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="رفع صورة كأيقونة", style=discord.ButtonStyle.gray, custom_id="role_panel_upload_icon")
    async def upload_icon_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._upload_icon_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد رفع أيقونة لها:"
        )

    async def _upload_icon_action(self, interaction: discord.Interaction, role_id: int):
        await interaction.response.send_message(
            "أرسل الصورة التي تريد استخدامها كأيقونة لرتبتك خلال 60 ثانية في هذه القناة "
            "(PNG أو JPG أو GIF، بحجم أقل من 256 كيلوبايت).",
            ephemeral=True
        )

        def check(m: discord.Message):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
                and len(m.attachments) > 0
            )

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await interaction.followup.send("انتهت المهلة، لم يتم استلام أي صورة.", ephemeral=True)
            return

        attachment = msg.attachments[0]
        if attachment.content_type not in ("image/png", "image/jpeg", "image/gif"):
            await interaction.followup.send("نوع الملف غير مدعوم. يرجى إرسال صورة PNG أو JPG أو GIF.", ephemeral=True)
            return

        try:
            image_bytes = await attachment.read()
        except Exception as e:
            await interaction.followup.send(f"تعذر تحميل الصورة: {e}", ephemeral=True)
            return

        # فحص الصورة عن وجود محتوى عري/غير لائق قبل قبولها
        is_nsfw, score = await is_image_nsfw(image_bytes)

        # نحذف الرسالة فوراً بمجرد ما نسحب بيانات الصورة منها، قبل ما نبلش نطبقها كأيقونة
        try:
            await msg.delete()
        except Exception:
            pass

        if is_nsfw:
            await interaction.followup.send(
                "❌ تم رفض هذه الصورة لأنها تحتوي على محتوى غير لائق. الرجاء اختيار صورة أخرى مناسبة.",
                ephemeral=True
            )
            return

        success, result_msg = await role_utils.do_edit_role(
            self.bot, interaction.guild, interaction.user, role_id, icon_bytes=image_bytes
        )
        await interaction.followup.send(result_msg, ephemeral=True)

    @discord.ui.button(label="استخدام إيموجي كأيقونة", style=discord.ButtonStyle.gray, custom_id="role_panel_emoji_icon")
    async def emoji_icon_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._emoji_icon_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد استخدام إيموجي لها:"
        )

    async def _emoji_icon_action(self, interaction: discord.Interaction, role_id: int):
        await interaction.response.send_message(
            "أرسل الإيموجي الذي تريد استخدامه كأيقونة لرتبتك خلال 60 ثانية في هذه القناة "
            "(إيموجي عادي أو إيموجي مخصص من هذا السيرفر).",
            ephemeral=True
        )

        def check(m: discord.Message):
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
                and m.content.strip() != ""
            )

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await interaction.followup.send("انتهت المهلة، لم يتم استلام أي إيموجي.", ephemeral=True)
            return

        emoji_value = msg.content

        # نحذف الرسالة فوراً بمجرد ما نسحب النص منها
        try:
            await msg.delete()
        except Exception:
            pass

        success, result_msg = await role_utils.do_edit_role(
            self.bot, interaction.guild, interaction.user, role_id, emoji=emoji_value
        )
        await interaction.followup.send(result_msg, ephemeral=True)

    @discord.ui.button(label="مدة الاشتراك", style=discord.ButtonStyle.gray, custom_id="role_panel_duration")
    async def duration_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _resolve_role_or_prompt(
            interaction, self._duration_action,
            prompt="لديك أكثر من رتبة اشتراك فعالة. اختر الرتبة التي تريد معرفة مدتها:"
        )

    async def _duration_action(self, interaction: discord.Interaction, role_id: int):
        import datetime
        rows = await asyncio.to_thread(db.get_owner_sub_info, interaction.guild.id, interaction.user.id)
        match = next((row for row in rows if row[0] == role_id), None)
        if not match:
            await interaction.response.send_message(
                "لا يوجد لديك أي اشتراك فعال حالياً. هذه الميزة مخصصة للمشتركين فقط.", ephemeral=True
            )
            return

        _, start_date_str, end_date_str = match

        if start_date_str:
            start_dt = datetime.datetime.strptime(start_date_str, '%Y-%m-%d %H:%M:%S')
            start_text = f"{start_dt.month}/{start_dt.day}"
        else:
            start_text = "غير معروف"

        end_dt = datetime.datetime.strptime(end_date_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        end_timestamp = int(end_dt.timestamp())

        msg = (
            f"اسم المشترك: {interaction.user.mention}\n"
            f"موعد الاشتراك: {start_text}\n"
            f"الوقت المتبقي حتى نهاية الاشتراك: <t:{end_timestamp}:R>"
        )
        await interaction.response.send_message(msg, ephemeral=True)


class RolePanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="send_role_panel", description="نشر لوحة تحكم الرتبة الخاصة في قناة معينة")
    @app_commands.describe(channel="القناة التي تريد نشر اللوحة فيها (اختياري وتكون افتراضياً القناة الحالية)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def send_role_panel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target_channel = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="لوحة تحكم الرتب الخاصة",
            description=(
                "إضافة عضو: يمكنك إضافة شخصين إلى رتبتك\n"
                "إزالة عضو: إزالة رتبتك الخاصة من عضو أضفته سابقاً\n"
                "تعديل الرتبة: تعديل اسم الرتبة الخاصة\n"
                "إزالة الأيقونة: إزالة الأيقونة الحالية الموضوعة على رتبتك الخاصة\n"
                "رفع صورة كأيقونة: استخدام صورة من جهازك كأيقونة لرتبتك\n"
                "استخدام إيموجي كأيقونة: استخدام إيموجي (من هذا السيرفر أو من خارجه) كأيقونة لرتبتك\n"
                "مدة الاشتراك: عرض تفاصيل اشتراكك والوقت المتبقي حتى انتهائه\n\n"
                f"ملاحظة: في حال رغبتك في لون جديد يرجى التوجه لـ"
                f"<#{config.TICKET_CHANNEL_ID}> و التواصل مع <@{config.ADMIN_USER_ID}>"
            ),
            color=discord.Color.blurple()
        )

        try:
            await target_channel.send(embed=embed, view=RolePanelView(self.bot))
            await interaction.followup.send(f"تم نشر اللوحة في القناة {target_channel.mention}.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"حدث خطأ أثناء نشر اللوحة: {e}", ephemeral=True)

    @send_role_panel.error
    async def send_role_panel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            message = "هذا الأمر مخصص للإدارة فقط."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RolePanelCog(bot))
