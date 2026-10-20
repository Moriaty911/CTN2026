"""Team-private reporting review UI. No platform client or privileged message intent."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import dataclass,field
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path

from .moderation.review import ReviewService
from .storage import StateStore


def _ids(value):
    result=frozenset(int(v.strip()) for v in value.split(',') if v.strip())
    if any(v<=0 for v in result):raise ValueError('Discord IDs must be positive')
    return result


@dataclass(frozen=True)
class DiscordConfig:
    token: str = field(repr=False)
    guild_id: int
    channel_id: int
    reviewer_users: frozenset[int] = frozenset()
    reviewer_roles: frozenset[int] = frozenset()
    admin_users: frozenset[int] = frozenset()
    admin_roles: frozenset[int] = frozenset()
    state_file: str = 'state/ctn.sqlite3'

    @classmethod
    def load(cls):
        # Intentionally does not load the fleet .env or platform/model credentials.
        cfg=cls(os.environ.get('CTN_DISCORD_TOKEN',''),int(os.environ.get('CTN_DISCORD_GUILD_ID','0')),int(os.environ.get('CTN_DISCORD_CHANNEL_ID','0')),
            *(_ids(os.environ.get('CTN_DISCORD_'+key,'')) for key in ('REVIEWER_USERS','REVIEWER_ROLES','ADMIN_USERS','ADMIN_ROLES')),
            os.environ.get('CTN_STATE_FILE','state/ctn.sqlite3'))
        if not cfg.token or cfg.guild_id<=0 or cfg.channel_id<=0 or not (cfg.admin_users or cfg.admin_roles):raise ValueError('Discord token, guild/channel and explicit administrator required')
        return cfg

    def authorize(self,guild,channel,user,roles=(),*,admin=False,read=False):
        if guild!=self.guild_id or channel!=self.channel_id:raise ValueError('operation restricted to the team review channel')
        if read:return
        administrator=user in self.admin_users or bool(set(roles)&self.admin_roles)
        reviewer=user in self.reviewer_users or bool(set(roles)&self.reviewer_roles)
        if not administrator and (admin or not reviewer):raise ValueError('insufficient review permission')


MAX_OUTBOX_ATTEMPTS=25


async def deliver_one(store,transport):
    """At-least-once notifications; business review/submission is independently idempotent."""
    job=await asyncio.to_thread(store.claim_discord_outbox)
    if job is None:return False
    log=logging.getLogger(__name__)
    try:payload=json.loads(job['payload_json'])
    except ValueError:payload={}
    attempts=int(job.get('attempts') or 0)+1
    reason=''
    if not isinstance(payload,dict):reason='payload is not an object'
    elif payload.get('type')=='candidate' and not str(payload.get('candidate_id') or '').strip():reason='candidate payload without candidate_id'
    elif attempts>=MAX_OUTBOX_ATTEMPTS:reason=f'{attempts} failed attempts'
    if reason:
        # Such a row can never be delivered; retrying it forever also starved the queue behind it
        # (19 empty-candidate_id notifications sat at the head on 2026-10-20).
        await asyncio.to_thread(store.abandon_discord_outbox,job,reason=reason)
        log.warning('Discord notification abandoned (%s)',reason)
        return True
    try:
        await transport(payload)
    except Exception:
        await asyncio.to_thread(store.finish_discord_outbox,job,ok=False)
        log.warning('Discord delivery deferred')
    else:await asyncio.to_thread(store.finish_discord_outbox,job,ok=True)
    return True


def build_bot(config,store):
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:raise RuntimeError('install requirements-discord.lock first') from exc
    from . import discord_ui as ui
    from .discord_session import PanelResponder
    import hashlib
    import time
    globals()['discord']=discord
    service=ReviewService(store)
    panels=PanelResponder()
    page_size=10

    def authorize(interaction,**kwargs):
        try:
            config.authorize(interaction.guild_id,interaction.channel_id,interaction.user.id,
                             [r.id for r in getattr(interaction.user,'roles',())],**kwargs)
        except ValueError as exc:
            if kwargs.get('admin') and str(exc)=='insufficient review permission':
                raise ValueError('administrator permission required') from None
            raise

    async def respond_error(interaction,exc):
        logging.getLogger(__name__).warning('Discord UI action failed: %s',type(exc).__name__)
        message=ui.error_message(exc)
        if interaction.guild_id==config.guild_id and interaction.channel_id==config.channel_id:
            await panels.error(interaction,message,view=HomeView())
        elif interaction.response.is_done():await interaction.followup.send(message,ephemeral=True)
        else:await interaction.response.send_message(message,ephemeral=True)

    async def acknowledge(interaction,*,admin=False,review=False):
        authorize(interaction,admin=admin,read=not (admin or review))
        await panels.defer(interaction)

    def button(view,label,action,custom_id,*,style=discord.ButtonStyle.secondary,row=None,disabled=False):
        item=discord.ui.Button(label=label,style=style,custom_id=custom_id,row=row,disabled=disabled)
        async def clicked(interaction):
            try:await action(interaction)
            except Exception as exc:await respond_error(interaction,exc)
        item.callback=clicked;view.add_item(item)
        return item

    def select_callback(item,action):
        async def selected(interaction):
            try:await action(interaction,item.values[0])
            except Exception as exc:await respond_error(interaction,exc)
        item.callback=selected

    async def show_home(interaction,*,controls=False,notice=None,ack=True):
        if ack:await acknowledge(interaction)
        else:authorize(interaction,read=True)
        data=await asyncio.to_thread(service.dashboard,limit=1)
        await panels.render(interaction,content=notice,embed=ui.dashboard(data,controls=controls),
                            view=ControlsView() if controls else HomeView())

    async def show_queue(interaction,offset=0,status='review'):
        await acknowledge(interaction)
        data=await asyncio.to_thread(service.dashboard,limit=page_size,offset=offset,status=status)
        if offset and offset>=data['candidate_total']:
            offset=max(0,(data['candidate_total']-1)//page_size*page_size)
            data=await asyncio.to_thread(service.dashboard,limit=page_size,offset=offset,status=status)
        await panels.render(interaction,embed=ui.queue(data),view=QueueView(data))

    async def show_candidate(interaction,ident,*,notice=None,ack=True):
        if ack:await acknowledge(interaction)
        else:authorize(interaction,read=True)
        data=await asyncio.to_thread(service.candidate,ident)
        await panels.render(interaction,content=notice,embed=ui.candidate(data),
                            view=CandidateView(ident,data['review']['revision'],data['review']['status']))
        return data

    async def show_evidence(interaction,ident,page=0):
        await acknowledge(interaction)
        data=await asyncio.to_thread(service.candidate,ident)
        entries=ui.evidence_entries(data);page=max(0,min(page,max(0,len(entries)-1)))
        await panels.render(interaction,embed=ui.evidence(data,page),view=EvidenceView(data,page))

    async def show_pending(interaction,page=0,*,notice=None):
        await acknowledge(interaction)
        data=await asyncio.to_thread(service.dashboard,limit=1)
        rows=data['pending'];pages=max(1,(len(rows)+page_size-1)//page_size);page=max(0,min(page,pages-1))
        selected=rows[page*page_size:(page+1)*page_size]
        embed=ui.pending(selected)
        embed.set_footer(text=f'Page {page+1} of {pages} · {len(rows)} unresolved submissions · A 24-hour delay never releases a pending slot.')
        await panels.render(interaction,content=notice,embed=embed,view=PendingView(selected,page,pages))

    async def show_attempt(interaction,ident):
        await acknowledge(interaction)
        row=await asyncio.to_thread(service.attempt,ident)
        await panels.render(interaction,embed=ui.attempt(row),view=AttemptView(row))

    async def show_help(interaction):
        await acknowledge(interaction)
        await panels.render(interaction,embed=ui.help_panel(),view=HomeView())

    async def decide(interaction,ident,revision,operation,reason=''):
        await acknowledge(interaction,review=True)
        await asyncio.to_thread(service.review,ident,operation,revision=revision,actor=str(interaction.user.id),
                                interaction_id=str(interaction.id),reason=reason)
        notices={
            'approve':'**Candidate approved for five minutes.** No report was submitted by this click. '
                      'Official availability and all remaining sending checks must also pass.',
            'reject':'**Candidate rejected.** The same evidence will not request another approval. New evidence needs a new review.',
            'defer':'**Review deferred for 30 minutes.** It will return to the queue automatically; this does not authorize submission.'}
        await show_candidate(interaction,ident,notice=notices[operation],ack=False)

    class HomeView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            async def refresh(interaction):
                source=getattr(interaction,'message',None)
                ref=store.policy('discord_control_panel',{})
                if source and not panels.private_source(interaction) and str(source.id)==ref.get('message_id'):
                    authorize(interaction,read=True)
                    await interaction.response.defer()
                    await bot.ensure_control_panel(force=True)
                else:await show_home(interaction)
            button(self,'Review queue',show_queue,'ctn:home:queue',style=discord.ButtonStyle.primary,row=0)
            button(self,'Pending reports',show_pending,'ctn:home:pending',row=0)
            button(self,'Reporting controls',lambda i:show_home(i,controls=True),'ctn:home:controls',row=1)
            button(self,'Refresh',refresh,'ctn:home:refresh',row=1)
            button(self,'Help',show_help,'ctn:home:help',row=1)

    class ControlsView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=600)
            async def clearance(interaction):
                authorize(interaction,admin=True)
                await interaction.response.send_modal(ClearanceModal())
            async def pause(interaction):
                await acknowledge(interaction,admin=True)
                await asyncio.to_thread(service.pause,str(interaction.user.id))
                await show_home(interaction,controls=True,ack=False,
                                notice='**New reports paused.** Existing pending reports remain unresolved. '
                                       'Confirm official availability again when it is appropriate to resume.')
            button(self,'Confirm availability',clearance,'ctn:controls:clearance',style=discord.ButtonStyle.success,row=0)
            button(self,'Pause new reports',pause,'ctn:controls:pause',style=discord.ButtonStyle.danger,row=0)
            button(self,'Refresh controls',lambda i:show_home(i,controls=True),'ctn:controls:refresh',row=1)
            button(self,'Main panel',show_home,'ctn:controls:home',row=1)

    class ClearanceModal(discord.ui.Modal):
        source=discord.ui.TextInput(label='Official source and when you checked it',
            placeholder='Reference the official reporting status and the time you actually checked it.',
            style=discord.TextStyle.paragraph,min_length=1,max_length=1000)
        def __init__(self):super().__init__(title='Confirm reporting availability')
        async def on_submit(self,interaction):
            try:
                await acknowledge(interaction,admin=True)
                await asyncio.to_thread(service.clearance,str(self.source),str(interaction.user.id))
                await show_home(interaction,controls=True,ack=False,
                                notice='**Official availability confirmed for 60 minutes.** This records your observation; '
                                       'the Bot did not independently verify it. Each candidate still needs approval. '
                                       'Disabled reporting and other sending restrictions remain in force.')
            except Exception as exc:await respond_error(interaction,exc)

    class QueueView(discord.ui.View):
        def __init__(self,data):
            super().__init__(timeout=600)
            if data['candidates']:
                options=[discord.SelectOption(label=(r['author']+' · '+('Account' if r['subject_kind']=='account' else 'Post'))[:100],
                          description=ui.label(r['status'])[:100],value=r['id']) for r in data['candidates']]
                choose=discord.ui.Select(placeholder='Choose a candidate to inspect',options=options,row=0)
                select_callback(choose,lambda i,value:show_candidate(i,value));self.add_item(choose)
            filters=[('Awaiting review','review'),('Approved','approved'),('Deferred','deferred'),('Rejected','rejected'),('All history','all')]
            menu=discord.ui.Select(placeholder='Filter candidates',row=1,options=[discord.SelectOption(label=name,value=value,
                    default=(data['filter']==value or data['filter'] is None and value=='all')) for name,value in filters])
            select_callback(menu,lambda i,value:show_queue(i,0,None if value=='all' else value));self.add_item(menu)
            offset=data['offset'];status=data['filter']
            button(self,'Previous',lambda i:show_queue(i,max(0,offset-page_size),status),'ctn:queue:prev',row=2,disabled=offset==0)
            button(self,'Next',lambda i:show_queue(i,offset+page_size,status),'ctn:queue:next',row=2,disabled=offset+page_size>=data['candidate_total'])
            button(self,'Refresh list',lambda i:show_queue(i,offset,status),'ctn:queue:refresh',row=2)
            button(self,'Main panel',show_home,'ctn:queue:home',row=2)

    class ReviewModal(discord.ui.Modal):
        explanation=discord.ui.TextInput(label='Why are you making this decision?',style=discord.TextStyle.paragraph,
                    placeholder='Explain what is missing or why this evidence should not be used.',max_length=1000)
        def __init__(self,ident,revision,operation):
            super().__init__(title='Reject this candidate' if operation=='reject' else 'Defer review for 30 minutes')
            self.ident,self.revision,self.operation=ident,revision,operation
        async def on_submit(self,interaction):
            try:await decide(interaction,self.ident,self.revision,self.operation,str(self.explanation))
            except Exception as exc:await respond_error(interaction,exc)

    class CandidateView(discord.ui.View):
        def __init__(self,ident,revision,status='review'):
            super().__init__(timeout=None)
            for operation,name,style in [('approve','Approve for 5 min',discord.ButtonStyle.success),
                                         ('reject','Reject',discord.ButtonStyle.danger),('defer','Defer 30 min',discord.ButtonStyle.secondary)]:
                async def review(interaction,op=operation):
                    authorize(interaction)
                    if op!='approve':await interaction.response.send_modal(ReviewModal(ident,revision,op))
                    else:await decide(interaction,ident,revision,op)
                button(self,name,review,f'ctn:{operation}:{ident}:{revision}',style=style,row=0,disabled=status!='review')
            button(self,'Evidence',lambda i:show_evidence(i,ident),f'ctn:evidence:{ident}:{revision}',row=1)
            button(self,'Refresh candidate',lambda i:show_candidate(i,ident),f'ctn:refresh:{ident}:{revision}',row=1)
            button(self,'Review queue',show_queue,f'ctn:queue:{ident}:{revision}',row=2)
            button(self,'Main panel',show_home,f'ctn:home:{ident}:{revision}',row=2)

    class EvidenceView(discord.ui.View):
        def __init__(self,data,page):
            super().__init__(timeout=600)
            ident=data['id'];size=len(ui.evidence_entries(data))
            button(self,'Previous evidence',lambda i:show_evidence(i,ident,page-1),'ctn:evidence:prev',row=0,disabled=page==0)
            button(self,'Next evidence',lambda i:show_evidence(i,ident,page+1),'ctn:evidence:next',row=0,disabled=page+1>=size)
            async def download(interaction):
                await acknowledge(interaction)
                latest=await asyncio.to_thread(service.candidate,ident)
                body=ui.evidence_text(latest).encode()
                if len(body)>1_500_000:raise ValueError('evidence export too large')
                await panels.render(interaction,content='Evidence is attached as readable text. Downloading it does not approve a report.',
                    embed=ui.evidence(latest,page),view=EvidenceView(latest,page),
                    attachments=[discord.File(io.BytesIO(body),filename='report-evidence.txt')])
            button(self,'Download text',download,'ctn:evidence:download',row=1)
            button(self,'Back to candidate',lambda i:show_candidate(i,ident),'ctn:evidence:back',row=1)

    class PendingView(discord.ui.View):
        def __init__(self,rows,page,pages):
            super().__init__(timeout=600)
            if rows:
                menu=discord.ui.Select(placeholder='Choose a submission to inspect',row=0,options=[
                    discord.SelectOption(label=(r['author']+' · '+('Account' if r['subject_kind']=='account' else 'Post'))[:100],
                                         description=ui.label(r['status'])[:100],value=r['id']) for r in rows])
                select_callback(menu,lambda i,value:show_attempt(i,value));self.add_item(menu)
            button(self,'Previous',lambda i:show_pending(i,page-1),'ctn:pending:prev',row=1,disabled=page==0)
            button(self,'Next',lambda i:show_pending(i,page+1),'ctn:pending:next',row=1,disabled=page+1>=pages)
            button(self,'Refresh pending',lambda i:show_pending(i,page),'ctn:pending:refresh',row=1)
            button(self,'Main panel',show_home,'ctn:pending:home',row=1)

    class AttemptView(discord.ui.View):
        def __init__(self,row):
            super().__init__(timeout=600)
            outcomes=[('upheld','Report upheld',discord.ButtonStyle.success)]
            outcomes.append(('rejected','Post ruled genuine',discord.ButtonStyle.danger) if row['subject_kind']=='post' else
                            ('dismissed','Account report dismissed',discord.ButtonStyle.secondary))
            for outcome,name,style in outcomes:
                async def prepare(interaction,value=outcome):
                    authorize(interaction,admin=True)
                    await interaction.response.send_modal(RulingModal(row['id'],value))
                button(self,name,prepare,'ctn:attempt:'+outcome,style=style,row=0,disabled=row['status'] not in ('pending','unknown'))
            button(self,'Refresh submission',lambda i:show_attempt(i,row['id']),'ctn:attempt:refresh',row=1)
            button(self,'Pending reports',show_pending,'ctn:attempt:pending',row=1)
            button(self,'Main panel',show_home,'ctn:attempt:home',row=1)

    async def preview_ruling(interaction,ident,outcome,ruled_at,source,*,ack=True):
        if ack:await acknowledge(interaction,admin=True)
        else:authorize(interaction,admin=True)
        try:at=datetime.fromisoformat(ruled_at.replace('Z','+00:00'))
        except ValueError:raise ValueError('invalid ruling timestamp') from None
        if at.tzinfo is None:raise ValueError('ruling time must include timezone')
        event=await asyncio.to_thread(service.prepare_ruling,ident,outcome,at.timestamp(),source)
        await panels.render(interaction,embed=ui.ruling(event),view=RulingConfirm(event,interaction.user.id))

    class RulingModal(discord.ui.Modal):
        official_source=discord.ui.TextInput(label='Official decision source / reference',style=discord.TextStyle.paragraph,
                    placeholder='Enter the official result reference, not your own assessment.',max_length=1000)
        official_time=discord.ui.TextInput(label='Official decision time, including time zone',
                    placeholder='Example: 2026-10-20T14:30:00+11:00',max_length=100)
        def __init__(self,ident,outcome):
            super().__init__(title='Record official ruling: '+outcome)
            self.ident,self.outcome=ident,outcome
        async def on_submit(self,interaction):
            try:await preview_ruling(interaction,self.ident,self.outcome,str(self.official_time),str(self.official_source))
            except Exception as exc:await respond_error(interaction,exc)

    class RulingConfirm(discord.ui.View):
        def __init__(self,event,owner):
            super().__init__(timeout=120);self.event,self.owner=event,owner
        @discord.ui.button(label='Confirm official ruling',style=discord.ButtonStyle.danger)
        async def confirm(self,interaction,item):
            try:
                authorize(interaction,admin=True)
                if interaction.user.id!=self.owner:raise ValueError('confirmation belongs to the administrator who prepared it')
                await acknowledge(interaction,admin=True)
                applied=await asyncio.to_thread(service.ruling,self.event,str(interaction.user.id))
                item.disabled=True
                effect='The team-wide six-hour suspension is now enforced.' if self.event['outcome']=='rejected' else 'The submission is resolved using this official result.'
                row=await asyncio.to_thread(service.attempt,self.event['attempt_id'])
                await panels.render(interaction,content=('**Official ruling recorded.** ' if applied else '**This ruling was already recorded.** ')+effect,
                                    embed=ui.attempt(row),view=AttemptView(row))
                self.stop()
            except Exception as exc:await respond_error(interaction,exc)
        @discord.ui.button(label='Cancel preview',style=discord.ButtonStyle.secondary)
        async def cancel(self,interaction,item):
            try:
                authorize(interaction,admin=True)
                if interaction.user.id!=self.owner:raise ValueError('confirmation belongs to the administrator who prepared it')
                await acknowledge(interaction,admin=True)
                for child in self.children:child.disabled=True
                await show_home(interaction,notice='Preview cancelled. No ruling was recorded.',ack=False)
                self.stop()
            except Exception as exc:await respond_error(interaction,exc)

    class ReviewBot(discord.Client):
        def __init__(self):
            intents=discord.Intents.none();intents.guilds=True
            super().__init__(intents=intents,allowed_mentions=discord.AllowedMentions.none())
            self.tree=app_commands.CommandTree(self);self.outbox_task=None
            self.panel_lock=asyncio.Lock();self.panel_message=None;self.panel_signature=None;self.panel_error=None;self.panel_checked_at=0
        async def setup_hook(self):
            self.add_view(HomeView())
            with store._lock:rows=store._conn.execute('SELECT * FROM discord_messages').fetchall()
            for row in rows:self.add_view(CandidateView(row['candidate_id'],row['revision']),message_id=int(row['message_id']))
            await self.tree.sync(guild=discord.Object(id=config.guild_id))
            self.outbox_task=asyncio.create_task(self.outbox())
        async def close(self):
            if self.outbox_task:
                self.outbox_task.cancel();await asyncio.gather(self.outbox_task,return_exceptions=True)
            await super().close()
        async def ensure_control_panel(self,*,force=False):
            async with self.panel_lock:
                data=await asyncio.to_thread(service.dashboard,limit=1)
                embed=ui.dashboard(data)
                signature=hashlib.sha256(json.dumps(embed.to_dict(),sort_keys=True).encode()).hexdigest()
                if not force and self.panel_message and self.panel_signature==signature and time.time()-self.panel_checked_at<300:return
                channel=await self.fetch_channel(config.channel_id)
                if not getattr(channel,'guild',None) or channel.guild.id!=config.guild_id:raise ValueError('configured channel is outside team guild')
                message=self.panel_message
                if not message:
                    ref=store.policy('discord_control_panel',{})
                    if ref.get('channel_id')==str(channel.id) and ref.get('message_id'):
                        try:message=await channel.fetch_message(int(ref['message_id']))
                        except discord.NotFound:pass
                        if message and message.author.id!=self.user.id:message=None
                    if not message:
                        # Recover an accepted-but-timed-out send before creating another panel.
                        async for prior in channel.history(limit=30):
                            if prior.author.id==self.user.id and prior.embeds and prior.embeds[0].title==embed.title:
                                message=prior;break
                if message:
                    try:await message.edit(embed=embed,view=HomeView())
                    except discord.NotFound:message=None
                if not message:message=await channel.send(embed=embed,view=HomeView())
                store.set_policy('discord_control_panel',{'channel_id':str(channel.id),'message_id':str(message.id)})
                self.panel_message=message;self.panel_signature=signature;self.panel_error=None;self.panel_checked_at=time.time()
        async def outbox(self):
            await self.wait_until_ready();next_panel=0
            log=logging.getLogger(__name__)
            while not self.is_closed():
                try:
                    await panels.expire_idle()
                    await asyncio.to_thread(store.refresh_report_reviews)
                    now=time.time()
                    if now>=next_panel:
                        try:await self.ensure_control_panel()
                        except Exception as exc:
                            self.panel_error=type(exc).__name__
                            log.warning('Control panel refresh failed: %s',self.panel_error)
                        next_panel=now+30
                    if store.policy('discord_health',{}).get('at',0)<time.time()-20:
                        await asyncio.to_thread(store.set_policy,'discord_health',{'at':time.time(),'connected':self.is_ready(),'panel_error':self.panel_error})
                    if not await deliver_one(store,self.deliver):await asyncio.sleep(2)
                except Exception as exc:
                    # This task ended for good on 2026-10-20 (a database lock raised inside
                    # refresh_report_reviews). The bot stayed connected, so nothing restarted it and
                    # every later notification queued behind the corpse. Keep the loop alive instead.
                    log.warning('Outbox tick failed: %s: %s',type(exc).__name__,exc)
                    await asyncio.sleep(10)
        async def deliver(self,payload):
            channel=await self.fetch_channel(config.channel_id)
            if not getattr(channel,'guild',None) or channel.guild.id!=config.guild_id:raise ValueError('configured channel is outside team guild')
            if payload['type']=='candidate':
                data=await asyncio.to_thread(service.candidate,payload['candidate_id'])
                view=CandidateView(data['id'],data['review']['revision'],data['review']['status'])
                with store._lock:prior=store._conn.execute('SELECT * FROM discord_messages WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1',(data['id'],)).fetchone()
                message=None
                if prior:
                    try:message=await channel.fetch_message(int(prior['message_id']))
                    except discord.NotFound:pass
                if message:await message.edit(embed=ui.candidate(data),view=view)
                else:message=await channel.send(embed=ui.candidate(data),view=view)
                with store.batch_transaction():store._conn.execute('INSERT INTO discord_messages VALUES(?,?,?,?) ON CONFLICT(candidate_id,message_id) DO UPDATE SET revision=excluded.revision',
                                        (data['id'],str(message.id),str(channel.id),data['review']['revision']))
            elif payload['type']=='aged_pending':
                row=await asyncio.to_thread(service.attempt,payload['attempt_id'])
                await channel.send('**Official ruling overdue.** This report has been pending for more than 24 hours. '
                                   'Check the official result; its slot stays occupied until resolved.',embed=ui.attempt(row),view=AttemptView(row))
            elif payload['type']=='ruling':
                await channel.send('**An official ruling was recorded.** See its source and effect below.',embed=ui.ruling(payload['event'],preview=False))
            elif payload['type']=='alert' and payload.get('kind')=='unknown_burst':
                await channel.send(f"**Uncertain sends: {int(payload['accounts'])} accounts in {int(payload['window_seconds']//60)} minutes.** "
                                   'The platform may be timing out or failing. Each send is checked automatically and released within 15 minutes; '
                                   'run `python -m ctn.ops status` to see the list.')
            elif payload['type']=='alert' and payload.get('kind')=='news_digest':
                s=payload['summary']
                lines=[f"**News digest.** Articles: {s['articles']}. Claims: {s['claims']}."]
                if s['helps_yes']:lines+=['Helps YES:']+['• '+t for t in s['helps_yes']]
                if s['hurts_yes']:lines+=['Hurts YES:']+['• '+t for t in s['hurts_yes']]
                await channel.send('\n'.join(lines)[:1900])
            elif payload['type']=='alert' and str(payload.get('kind','')).startswith('news_'):
                labels={'news_conflict':'Conflicting figures in the news; these claims are not used until resolved',
                        'news_article_quarantined':'An article was quarantined by the injection check and will not be used',
                        'news_extraction_failed':'Claim extraction failed three times for an article'}
                await channel.send(f"**{labels.get(payload['kind'],payload['kind'])}.** `{json.dumps(payload.get('detail',{}),ensure_ascii=False)[:1500]}`")
            elif payload['type']=='alert' and payload.get('kind') in ('self_check','own_post_hidden','account_slowed'):
                text={'self_check':f"**Self-check: `{payload.get('account_id')}` looks bot-like to a detector.** {json.dumps(payload.get('detail',{}))}",
                      'own_post_hidden':f"**One of our posts looks hidden or removed** (`{payload.get('account_id')}`, post `{payload.get('post_id')}`): {json.dumps(payload.get('fields',{}),default=str)[:800]}. Three permanently hidden posts suspend an account.",
                      'account_slowed':f"**`{payload.get('account_id')}` slowed to {payload.get('daily_cap')} posts a day** after {payload.get('hidden_posts')} hidden posts."}[payload['kind']]
                await channel.send(text[:1900])
            elif payload['type']=='alert' and payload.get('kind')=='initialise_failing':
                await channel.send(f"**Initialise post rejected twice for `{payload['account_id']}`** (last reason: {payload['reason']}). "
                                   'The next attempt uses a different daily example; check the persona examples if this continues.')

    bot=ReviewBot();guild=discord.Object(id=config.guild_id)

    @bot.tree.command(name='reports',description='Open the report dashboard with review, pending and help buttons',guild=guild)
    async def reports(interaction:discord.Interaction):
        try:await show_home(interaction)
        except Exception as exc:await respond_error(interaction,exc)

    @bot.tree.command(name='report',description='Open the review queue, or inspect an optional candidate ID',guild=guild)
    async def report(interaction:discord.Interaction,candidate_id:str=''):
        try:
            if candidate_id:await show_candidate(interaction,candidate_id)
            else:await show_queue(interaction)
        except Exception as exc:await respond_error(interaction,exc)

    @bot.tree.command(name='reporting',description='Open explained reporting controls; use the buttons for routine actions',guild=guild)
    @app_commands.choices(operation=[app_commands.Choice(name=name,value=value) for name,value in
            [('View controls','status'),('Confirm availability','clearance'),('Pause new reports','pause')]])
    async def reporting(interaction:discord.Interaction,operation:str='status',source:str=''):
        try:
            if operation=='clearance':
                authorize(interaction,admin=True)
                if not source:await interaction.response.send_modal(ClearanceModal());return
                await acknowledge(interaction,admin=True)
                await asyncio.to_thread(service.clearance,source,str(interaction.user.id))
                await show_home(interaction,controls=True,ack=False,notice='**Availability confirmed for 60 minutes.** '
                                'Individual approval and other sending checks still apply; disabled reporting stays disabled.')
            elif operation=='pause':
                await acknowledge(interaction,admin=True)
                await asyncio.to_thread(service.pause,str(interaction.user.id))
                await show_home(interaction,controls=True,ack=False,notice='**New reports paused.** Existing pending submissions remain unresolved.')
            else:await show_home(interaction,controls=True)
        except Exception as exc:await respond_error(interaction,exc)

    @bot.tree.command(name='ruling',description='Choose a pending submission and record its official outcome with a form',guild=guild)
    @app_commands.choices(outcome=[app_commands.Choice(name=name,value=value) for name,value in
            [('Report upheld','upheld'),('Post ruled genuine','rejected'),('Account report dismissed','dismissed')]])
    async def ruling(interaction:discord.Interaction,attempt_id:str='',outcome:str='',ruled_at:str='',source:str=''):
        try:
            if not any((attempt_id,outcome,ruled_at,source)):await show_pending(interaction);return
            if not all((attempt_id,outcome,ruled_at,source)):
                await show_pending(interaction,notice='**No ruling was recorded.** Some details were missing. Select a submission below and use the guided form.');return
            await preview_ruling(interaction,attempt_id,outcome,ruled_at,source)
        except Exception as exc:await respond_error(interaction,exc)
    return bot


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check-config',action='store_true');parser.add_argument('--healthcheck',action='store_true');args=parser.parse_args(argv)
    if args.healthcheck:
        from contextlib import closing
        import time
        with closing(StateStore(os.environ.get('CTN_STATE_FILE','state/ctn.sqlite3'),migrate=False)) as store:health=store.policy('discord_health',{})
        raise SystemExit(0 if health.get('connected') and not health.get('panel_error') and health.get('at',0)>time.time()-60 else 1)
    config=DiscordConfig.load()
    if args.check_config:
        print(json.dumps({'configured':True,'guild_id':config.guild_id,'channel_id':config.channel_id}));return
    from contextlib import closing
    with closing(StateStore(config.state_file,migrate=False)) as store:build_bot(config,store).run(config.token)

if __name__=='__main__':main()
