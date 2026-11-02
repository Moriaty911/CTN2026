"""Button-driven reporting workflows against real SQLite; no network or credentials."""
import asyncio
from contextlib import closing
from datetime import datetime,timezone
import re
import itertools
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock,Mock

try:
    import discord
except ImportError:
    discord=None

from ctn.discord_bot import DiscordConfig,build_bot
from ctn.moderation.review import ReviewService
from ctn.storage import StateStore
from tests.test_campaign_v5 import strong_evidence


_interaction_ids=itertools.count(1000)
_message_ids=itertools.count(10000)


def panel_message(*,private=True,ident=None):
    message=SimpleNamespace(id=ident or next(_message_ids),flags=SimpleNamespace(ephemeral=private),payload={})
    async def edit(**kwargs):message.payload.update(kwargs);return message
    message.edit=AsyncMock(side_effect=edit);message.delete=AsyncMock()
    return message


def interaction(user=4,*,ident=None,guild=1,channel=2,source=None):
    response=Mock()
    response.defer=AsyncMock();response.send_message=AsyncMock();response.send_modal=AsyncMock()
    response.is_done.return_value=False
    event=SimpleNamespace(guild_id=guild,channel_id=channel,user=SimpleNamespace(id=user,roles=[]),
        response=response,followup=SimpleNamespace(send=AsyncMock()),id=ident or next(_interaction_ids),
        type=discord.InteractionType.component if source else discord.InteractionType.application_command,message=source)
    original=panel_message()
    event.result_message=original
    async def defer(**kwargs):
        response.is_done.return_value=True
        response.type=discord.InteractionResponseType.deferred_message_update if source and not kwargs.get('thinking') else discord.InteractionResponseType.deferred_channel_message
    async def edit(**kwargs):
        target=source if response.type==discord.InteractionResponseType.deferred_message_update else original
        target.payload.update(kwargs);event.result_message=target;return target
    async def delete():event.result_message.deleted=True
    async def followup(*args,**kwargs):
        message=panel_message();message.payload.update(kwargs);event.result_message=message;return message
    response.defer.side_effect=defer
    response.type=None
    event.edit_original_response=AsyncMock(side_effect=edit)
    event.delete_original_response=AsyncMock(side_effect=delete)
    event.followup.send.side_effect=followup
    return event


def find_button(view,label):
    return next(child for child in view.children if getattr(child,'label',None)==label)


def sent(inter):return inter.edit_original_response.await_args.kwargs if inter.edit_original_response.await_count else inter.followup.send.await_args.kwargs


async def empty_history():
    if False:yield None


@unittest.skipIf(discord is None,'optional Discord dependency')
class DiscordPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.store.set_policy('report_mode','manual')
        self.cfg=DiscordConfig('synthetic',1,2,reviewer_users=frozenset({3}),admin_users=frozenset({4,5}))
        self.bot=build_bot(self.cfg,self.store)
        self.addAsyncCleanup(self.bot.close)

    def command(self,name):
        return next(c for c in self.bot.tree.get_commands(guild=discord.Object(id=1)) if c.name==name)

    def candidate(self):return self.store.store_report_candidate(strong_evidence(self.store))

    async def home(self):
        event=interaction();await self.command('reports').callback(event)
        return sent(event)['view']

    async def open_candidate(self,ident,user=4):
        event=interaction(user);await self.command('report').callback(event,ident)
        return event

    async def controls(self):
        event=interaction();await self.command('reporting').callback(event)
        return sent(event)['view']

    def add_attempt(self):
        self.candidate()
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO report_attempts VALUES('attempt','reporter','post','leak','enemy','','pending',?,0,'','{}')",(time.time()-60,))

    async def attempt_view(self):
        event=interaction();await self.command('ruling').callback(event)
        menu=next(child for child in sent(event)['view'].children if isinstance(child,discord.ui.Select))
        menu._values=['attempt'];select=interaction();await menu.callback(select)
        return sent(select)['view']

    async def test_dashboard_is_explained_english_with_buttons_and_no_file(self):
        self.store.set_policy('report_mode','off')
        event=interaction(9);await self.command('reports').callback(event)
        event.response.defer.assert_awaited_once()
        result=sent(event)
        self.assertNotIn('file',result)
        self.assertIn('Review queue',result['embed'].description)
        self.assertIn('no reports will be submitted',str(result['embed'].to_dict()))
        self.assertEqual([c.label for c in result['view'].children],['Review queue','Pending reports','Reporting controls','Refresh','Help'])
        self.assertEqual(event.response.defer.await_args.kwargs,{'ephemeral':True,'thinking':True})
        event.followup.send.assert_not_awaited()
        self.assertIsNone(re.search('[\u4e00-\u9fff]',str(result['embed'].to_dict())))

    async def test_select_candidate_read_evidence_and_approve_without_copying_ids(self):
        ident=self.candidate();home=await self.home()
        opening=interaction();await find_button(home,'Review queue').callback(opening)
        menu=next(child for child in sent(opening)['view'].children if isinstance(child,discord.ui.Select) and child.row==0)
        menu._values=[ident];selected=interaction(3);await menu.callback(selected)
        view=sent(selected)['view']
        evidence=interaction(3);await find_button(view,'Evidence').callback(evidence)
        self.assertIn('Evidence 1 of',sent(evidence)['embed'].title)
        self.assertNotIn('file',sent(evidence))
        next_page=interaction(3);await find_button(sent(evidence)['view'],'Next evidence').callback(next_page)
        self.assertIn('Evidence 2 of',sent(next_page)['embed'].title)
        approved=interaction(3,ident=101);await find_button(view,'Approve for 5 min').callback(approved)
        self.assertEqual(self.store.report_review(ident)['status'],'approved')
        self.assertFalse(self.store.report_state_ready()[0])
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM action_intents').fetchone()[0],0)
        self.assertIn('No report was submitted',sent(approved)['content'])
        self.assertTrue(find_button(sent(approved)['view'],'Approve for 5 min').disabled)
        self.assertFalse(find_button(sent(approved)['view'],'Evidence').disabled)

    async def test_old_candidate_click_cannot_create_second_approval(self):
        ident=self.candidate();opened=await self.open_candidate(ident,3);view=sent(opened)['view']
        await find_button(view,'Approve for 5 min').callback(interaction(3,ident=200))
        second=interaction(3,ident=201);await find_button(view,'Approve for 5 min').callback(second)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM report_approvals').fetchone()[0],1)
        self.assertIn('Refresh',sent(second)['content'])

    async def test_reviewer_cannot_change_team_controls_and_wrong_channel_cannot_navigate(self):
        controls=await self.controls()
        denied=interaction(3);await find_button(controls,'Pause new reports').callback(denied)
        self.assertFalse(self.store.policy('reporting_paused',False))
        self.assertIn('Only a configured administrator',sent(denied)['content'])
        modal=interaction(3);await find_button(controls,'Confirm availability').callback(modal)
        modal.response.send_modal.assert_not_awaited()
        wrong=interaction(4,channel=999);await find_button(await self.home(),'Review queue').callback(wrong)
        self.assertIn('configured team review channel',wrong.response.send_message.await_args.args[0])

    async def test_clearance_form_rechecks_admin_and_pause_preserves_pending(self):
        self.add_attempt();controls=await self.controls()
        opening=interaction();await find_button(controls,'Confirm availability').callback(opening)
        modal=opening.response.send_modal.await_args.args[0]
        modal.source._value='Synthetic official status observation and time'
        await modal.on_submit(interaction(3))
        self.assertFalse(self.store.report_state_ready()[0])
        accepted=interaction();await modal.on_submit(accepted)
        self.assertTrue(self.store.report_state_ready()[0])
        self.assertFalse(self.store.report_state_ready(automatic=True)[0])
        self.assertIn('did not independently verify',sent(accepted)['content'])
        await find_button(controls,'Pause new reports').callback(interaction())
        self.assertTrue(self.store.policy('reporting_paused'))
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_attempts WHERE id='attempt'").fetchone()[0],'pending')

    async def test_suspension_blocks_clearance_with_human_explanation(self):
        self.store.set_counter('report_suspended_until_ms',int((time.time()+600)*1000))
        opening=interaction();await find_button(await self.controls(),'Confirm availability').callback(opening)
        modal=opening.response.send_modal.await_args.args[0];modal.source._value='Synthetic official record'
        submit=interaction();await modal.on_submit(submit)
        self.assertIn('six-hour suspension',sent(submit)['content'])
        self.assertFalse(self.store.policy('report_clearance',{}))

    async def test_defer_form_records_reason_and_explains_return_time(self):
        ident=self.candidate();opened=await self.open_candidate(ident)
        opening=interaction(3);await find_button(sent(opened)['view'],'Defer 30 min').callback(opening)
        modal=opening.response.send_modal.await_args.args[0]
        modal.explanation._value='Wait for a complete source post.'
        submit=interaction(3,ident=300);await modal.on_submit(submit)
        row=self.store.report_review(ident)
        self.assertEqual(row['status'],'deferred')
        self.assertGreater(row['resume_at'],time.time()+1790)
        self.assertIn('return to the queue automatically',sent(submit)['content'])

    async def test_pending_ruling_uses_form_then_owner_bound_confirmation(self):
        self.add_attempt();view=await self.attempt_view()
        opening=interaction();await find_button(view,'Report upheld').callback(opening)
        modal=opening.response.send_modal.await_args.args[0]
        modal.official_source._value='Synthetic official moderation decision'
        modal.official_time._value=datetime.now(timezone.utc).isoformat()
        preview=interaction();await modal.on_submit(preview)
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_attempts WHERE id='attempt'").fetchone()[0],'pending')
        self.assertNotIn('file',sent(preview))
        confirm=sent(preview)['view']
        other=interaction(5);await find_button(confirm,'Confirm official ruling').callback(other)
        self.assertIn('Only the administrator',sent(other)['content'])
        await find_button(confirm,'Confirm official ruling').callback(interaction(4))
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_attempts WHERE id='attempt'").fetchone()[0],'ruled')

    async def test_invalid_official_time_does_not_record_ruling(self):
        self.add_attempt();opening=interaction();await find_button(await self.attempt_view(),'Post ruled genuine').callback(opening)
        modal=opening.response.send_modal.await_args.args[0]
        modal.official_source._value='Synthetic decision';modal.official_time._value='not a time'
        submit=interaction();await modal.on_submit(submit)
        self.assertIn('ISO format',sent(submit)['content'])
        self.assertEqual(self.store._conn.execute("SELECT status FROM report_attempts WHERE id='attempt'").fetchone()[0],'pending')
        self.assertFalse(self.store.is_reporting_suspended()[0])

    async def test_queue_pagination_counts_all_candidates_and_filters(self):
        for i in range(31):self.store.store_report_candidate(strong_evidence(self.store,author=f'enemy{i}',target=f'target{i}'))
        service=ReviewService(self.store)
        first=service.dashboard(limit=10,status='review')
        second=service.dashboard(limit=10,offset=10,status='review')
        self.assertEqual(first['candidate_total'],31)
        self.assertEqual(first['candidate_counts']['review'],31)
        self.assertFalse({r['id'] for r in first['candidates']}&{r['id'] for r in second['candidates']})
        self.assertEqual(service.dashboard(status='deferred')['candidate_total'],0)
        with self.assertRaises(ValueError):service.dashboard(status="review' OR 1=1")

    async def test_candidate_notifications_use_cards_without_default_json_attachments(self):
        ident=self.candidate()
        message=SimpleNamespace(id=500)
        channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),send=AsyncMock(return_value=message))
        self.bot.fetch_channel=AsyncMock(return_value=channel)
        await self.bot.deliver({'type':'candidate','candidate_id':ident})
        result=channel.send.await_args.kwargs
        self.assertNotIn('file',result)
        self.assertIn('Your next step',[field.name for field in result['embed'].fields])
        self.assertTrue(any(getattr(c,'label',None)=='Evidence' for c in result['view'].children))

    async def test_explicit_evidence_download_is_readable_text_with_signal_details(self):
        ident=self.candidate();opening=await self.open_candidate(ident)
        evidence=interaction();await find_button(sent(opening)['view'],'Evidence').callback(evidence)
        export=interaction();await find_button(sent(evidence)['view'],'Download text').callback(export)
        attachment=sent(export)['attachments'][0];body=attachment.fp.read().decode()
        self.assertEqual(attachment.filename,'report-evidence.txt')
        self.assertIn('Recorded signal details:',body)
        self.assertIn('As an AI',body)
        self.assertIn('Parent:',body)
        self.assertFalse(body.lstrip().startswith('{'))
        self.assertEqual(self.store.report_review(ident)['status'],'review')

    async def test_account_evidence_preserves_unknowns_and_large_content_fits_embeds(self):
        from ctn import discord_ui as ui
        ident=self.candidate();data=ReviewService(self.store).candidate(ident)
        data['subject_kind']='account';data['payload']['eligibility']={'activity':None,'upheld_posts':None}
        data['payload']['evidence'][0]['post']['text']='@everyone **untrusted text** '*1000
        embed=ui.candidate(data)
        self.assertLessEqual(len(embed),6000)
        for field in embed.fields:self.assertLessEqual(len(field.value),1024)
        self.assertIn('Confirmed actions: Unknown',str(embed.to_dict()))
        self.assertNotIn('@everyone',embed.description)
        evidence=ui.evidence(data)
        self.assertLessEqual(len(evidence),6000)
        self.assertLessEqual(len(evidence.description),4096)

    async def test_panel_is_reused_after_restart_and_no_chat_intents_are_requested(self):
        self.bot._connection.user=SimpleNamespace(id=777)
        message=SimpleNamespace(id=501,author=SimpleNamespace(id=777),edit=AsyncMock())
        channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),send=AsyncMock(return_value=message),
                                fetch_message=AsyncMock(return_value=message),history=lambda **kwargs:empty_history())
        self.bot.fetch_channel=AsyncMock(return_value=channel)
        await self.bot.ensure_control_panel();await self.bot.ensure_control_panel()
        channel.send.assert_awaited_once()
        self.assertEqual(self.store.policy('discord_control_panel')['message_id'],'501')
        second=build_bot(self.cfg,self.store);self.addAsyncCleanup(second.close)
        second._connection.user=SimpleNamespace(id=777);second.fetch_channel=AsyncMock(return_value=channel)
        await second.ensure_control_panel()
        channel.send.assert_awaited_once();message.edit.assert_awaited_once()
        self.assertTrue(second.intents.guilds)
        self.assertFalse(second.intents.message_content)
        self.assertFalse(second.intents.members)

    async def test_timeout_recovery_finds_existing_panel_before_another_send(self):
        self.bot._connection.user=SimpleNamespace(id=777)
        accepted=[]
        async def history(**kwargs):
            for item in accepted:yield item
        async def send(**kwargs):
            accepted.append(SimpleNamespace(id=502,author=SimpleNamespace(id=777),embeds=[kwargs['embed']],edit=AsyncMock()))
            raise TimeoutError('accepted response lost')
        channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),history=history,send=AsyncMock(side_effect=send))
        self.bot.fetch_channel=AsyncMock(return_value=channel)
        with self.assertRaises(TimeoutError):await self.bot.ensure_control_panel()
        self.assertIsNone(self.store.policy('discord_control_panel'))
        await self.bot.ensure_control_panel()
        channel.send.assert_awaited_once()
        self.assertEqual(self.store.policy('discord_control_panel')['message_id'],'502')


if __name__=='__main__':unittest.main()
