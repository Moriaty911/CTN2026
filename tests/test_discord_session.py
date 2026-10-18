"""One-message navigation, privacy, modal updates and webhook expiry recovery."""
import time
from datetime import datetime,timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from ctn.discord_bot import DiscordConfig,build_bot
from ctn.storage import StateStore
from tests.test_campaign_v5 import strong_evidence
from tests.test_discord_panels import discord,interaction,panel_message,find_button,empty_history

if discord is not None:
    from ctn.discord_session import PanelResponder


@unittest.skipIf(discord is None,'optional Discord dependency')
class SinglePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=StateStore();self.addCleanup(self.store.close)
        self.store.set_policy('report_mode','manual')
        self.cfg=DiscordConfig('synthetic',1,2,reviewer_users=frozenset({3}),admin_users=frozenset({4}))
        self.bot=build_bot(self.cfg,self.store);self.addAsyncCleanup(self.bot.close)
        self.bot._connection.user=SimpleNamespace(id=777)
        self.root=panel_message(private=False,ident=501);self.root.author=SimpleNamespace(id=777)
        async def send(**kwargs):self.root.payload.update(kwargs);return self.root
        self.channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),send=AsyncMock(side_effect=send),
            fetch_message=AsyncMock(return_value=self.root),history=lambda **kwargs:empty_history())
        self.bot.fetch_channel=AsyncMock(return_value=self.channel)
        await self.bot.ensure_control_panel()
        self.launcher=self.root.payload['view']

    async def launch(self,user=4,label='Review queue'):
        event=interaction(user,source=self.root)
        await find_button(self.launcher,label).callback(event)
        self.assertEqual(event.response.defer.await_args.kwargs,{'ephemeral':True,'thinking':True})
        event.followup.send.assert_not_awaited()
        return event.result_message

    def assert_in_place(self,event,workspace):
        self.assertIs(event.result_message,workspace)
        self.assertEqual(event.response.defer.await_args.kwargs,{})
        event.followup.send.assert_not_awaited()
        event.edit_original_response.assert_awaited_once()

    async def test_navigation_evidence_and_back_keep_the_same_private_message(self):
        ident=self.store.store_report_candidate(strong_evidence(self.store))
        workspace=await self.launch()
        menu=next(c for c in workspace.payload['view'].children if isinstance(c,discord.ui.Select) and c.row==0)
        menu._values=[ident];chosen=interaction(source=workspace);await menu.callback(chosen)
        self.assert_in_place(chosen,workspace)
        for name in ('Evidence','Next evidence','Previous evidence','Back to candidate','Review queue','Main panel','Help','Refresh'):
            event=interaction(source=workspace)
            await find_button(workspace.payload['view'],name).callback(event)
            self.assert_in_place(event,workspace)
        self.assertEqual(self.root.payload['embed'].title,'CTN · Report review')
        self.channel.send.assert_awaited_once()
        self.root.delete.assert_not_awaited()

    async def test_repeated_public_entry_reuses_workspace_and_isolates_users(self):
        first=await self.launch(4)
        second=await self.launch(3,label='Pending reports')
        self.assertNotEqual(first.id,second.id)
        for user,workspace in ((4,first),(3,second),(4,first)):
            event=interaction(user,source=self.root)
            await find_button(self.launcher,'Help').callback(event)
            self.assertEqual(event.response.defer.await_args.kwargs,{})
            event.edit_original_response.assert_not_awaited()
            event.followup.send.assert_not_awaited()
            self.assertEqual(workspace.payload['embed'].title,'How report review works')
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.root.payload['embed'].title,'CTN · Report review')

    async def test_defer_form_returns_to_the_same_candidate_panel(self):
        ident=self.store.store_report_candidate(strong_evidence(self.store))
        workspace=await self.launch()
        menu=next(c for c in workspace.payload['view'].children if isinstance(c,discord.ui.Select) and c.row==0)
        menu._values=[ident];chosen=interaction(source=workspace);await menu.callback(chosen)
        opening=interaction(source=workspace)
        await find_button(workspace.payload['view'],'Defer 30 min').callback(opening)
        opening.followup.send.assert_not_awaited()
        modal=opening.response.send_modal.await_args.args[0]
        modal.explanation._value='Wait for complete context.'
        submitted=interaction(source=workspace);submitted.type=discord.InteractionType.modal_submit
        await modal.on_submit(submitted)
        self.assert_in_place(submitted,workspace)
        self.assertEqual(self.store.report_review(ident)['status'],'deferred')
        self.assertIn('30 minutes',workspace.payload['content'])
        home=interaction(source=workspace);await find_button(workspace.payload['view'],'Main panel').callback(home)
        self.assertIsNone(workspace.payload['content'])

    async def test_permission_error_updates_content_without_replacing_controls(self):
        workspace=await self.launch(3,label='Reporting controls')
        original_view=workspace.payload['view'];original_embed=workspace.payload['embed']
        denied=interaction(3,source=workspace)
        await find_button(original_view,'Pause new reports').callback(denied)
        self.assert_in_place(denied,workspace)
        self.assertIn('Only a configured administrator',workspace.payload['content'])
        self.assertIs(workspace.payload['view'],original_view)
        self.assertIs(workspace.payload['embed'],original_embed)
        self.assertFalse(self.store.policy('reporting_paused',False))

    async def test_evidence_attachment_is_replaced_and_removed_on_navigation(self):
        ident=self.store.store_report_candidate(strong_evidence(self.store))
        workspace=await self.launch()
        menu=next(c for c in workspace.payload['view'].children if isinstance(c,discord.ui.Select) and c.row==0)
        menu._values=[ident];await menu.callback(interaction(source=workspace))
        await find_button(workspace.payload['view'],'Evidence').callback(interaction(source=workspace))
        export=interaction(source=workspace)
        await find_button(workspace.payload['view'],'Download text').callback(export)
        self.assert_in_place(export,workspace)
        self.assertEqual(workspace.payload['attachments'][0].filename,'report-evidence.txt')
        following=interaction(source=workspace)
        await find_button(workspace.payload['view'],'Next evidence').callback(following)
        self.assert_in_place(following,workspace)
        self.assertEqual(workspace.payload['attachments'],[])
        self.assertIsNone(workspace.payload['content'])

    async def test_ruling_preview_confirmation_and_result_stay_in_place(self):
        self.store.store_report_candidate(strong_evidence(self.store))
        with self.store.batch_transaction():
            self.store._conn.execute("INSERT INTO report_attempts VALUES('attempt','reporter','post','leak','enemy','','pending',?,0,'','{}')",(time.time()-60,))
        workspace=await self.launch(label='Pending reports')
        menu=next(c for c in workspace.payload['view'].children if isinstance(c,discord.ui.Select))
        menu._values=['attempt'];await menu.callback(interaction(source=workspace))
        opening=interaction(source=workspace)
        await find_button(workspace.payload['view'],'Post ruled genuine').callback(opening)
        modal=opening.response.send_modal.await_args.args[0]
        modal.official_source._value='Synthetic official decision'
        modal.official_time._value=datetime.now(timezone.utc).isoformat()
        preview=interaction(source=workspace);preview.type=discord.InteractionType.modal_submit
        await modal.on_submit(preview)
        self.assert_in_place(preview,workspace)
        self.assertEqual(workspace.payload['embed'].title,'Confirm official ruling')
        confirmed=interaction(source=workspace)
        await find_button(workspace.payload['view'],'Confirm official ruling').callback(confirmed)
        self.assert_in_place(confirmed,workspace)
        self.assertTrue(self.store.is_reporting_suspended()[0])
        self.assertIn('Official ruling recorded',workspace.payload['content'])

    async def test_explicit_slash_reopen_retires_previous_private_workspace(self):
        workspace=await self.launch()
        command=next(c for c in self.bot.tree.get_commands(guild=discord.Object(id=1)) if c.name=='reports')
        reopened=interaction();await command.callback(reopened)
        self.assertNotEqual(reopened.result_message.id,workspace.id)
        self.assertTrue(workspace.deleted)
        reopened.followup.send.assert_not_awaited()
        self.root.delete.assert_not_awaited()

    async def test_public_refresh_updates_only_the_shared_launcher(self):
        refresh=interaction(source=self.root)
        await find_button(self.launcher,'Refresh').callback(refresh)
        self.assertEqual(refresh.response.defer.await_args.kwargs,{})
        refresh.edit_original_response.assert_not_awaited()
        refresh.followup.send.assert_not_awaited()
        self.root.edit.assert_awaited_once()
        self.channel.send.assert_awaited_once()


@unittest.skipIf(discord is None,'optional Discord dependency')
class WorkspaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_private_webhook_creates_one_replacement_without_editing_public_source(self):
        router=PanelResponder();public=panel_message(private=False)
        first=interaction(source=public);await router.defer(first)
        old=await router.render(first,content='First panel')
        first.edit_original_response.side_effect=discord.NotFound(SimpleNamespace(status=404,reason='Not Found'),{'code':10015,'message':'Unknown Webhook'})
        again=interaction(source=public);await router.defer(again)
        replacement=await router.render(again,content='Replacement panel')
        self.assertNotEqual(old.id,replacement.id)
        again.edit_original_response.assert_not_awaited()
        again.followup.send.assert_awaited_once()
        self.assertEqual(len(router._sessions),1)
        self.assertEqual(len(router._routes),0)
        public.delete.assert_not_awaited()

    async def test_idle_cleanup_deletes_only_the_tracked_private_panel(self):
        router=PanelResponder();public=panel_message(private=False)
        event=interaction(source=public);await router.defer(event)
        await router.render(event,content='Private panel')
        await router.expire_idle(now=time.time()+601)
        event.delete_original_response.assert_awaited_once()
        self.assertFalse(router._sessions)
        public.delete.assert_not_awaited()


if __name__=='__main__':unittest.main()
