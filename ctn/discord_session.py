"""In-memory private panel routing. Interaction tokens never leave this process."""
from __future__ import annotations
import asyncio
import logging
import time

import discord


class PanelResponder:
    """Edit one private workspace per actor without navigating the shared launcher."""

    def __init__(self):
        self._sessions={}
        self._routes={}
        self._locks={}

    @staticmethod
    def key(interaction):return interaction.guild_id,interaction.channel_id,interaction.user.id

    @staticmethod
    def private_source(interaction):
        message=getattr(interaction,'message',None)
        return message if getattr(getattr(message,'flags',None),'ephemeral',False) is True else None

    def _prune(self):
        now=time.time()
        for key,session in list(self._sessions.items()):
            if session['until']<=now:
                self._sessions.pop(key,None)
                lock=self._locks.get(key)
                if lock and not lock.locked():self._locks.pop(key,None)
        for ident,route in list(self._routes.items()):
            if route['until']<=now:self._routes.pop(ident,None)

    async def defer(self,interaction):
        self._prune()
        key=self.key(interaction);source=self.private_source(interaction)
        if source:
            kind='source'
            await interaction.response.defer()
        elif getattr(interaction,'message',None) is not None and key in self._sessions:
            kind='reuse'
            await interaction.response.defer()
        else:
            kind='original'
            await interaction.response.defer(ephemeral=True,thinking=True)
        self._routes[interaction.id]={'kind':kind,'key':key,'until':time.time()+840}

    async def render(self,interaction,*,content=None,embed=None,view=None,attachments=None,preserve=False):
        route=self._routes.get(interaction.id)
        if route is None:
            # This path is for a silently acknowledged component whose cached
            # workspace disappeared. Never edit its public source message.
            route={'kind':'reuse','key':self.key(interaction),'until':time.time()+840}
        key=route['key']
        lock=self._locks.setdefault(key,asyncio.Lock())
        async with lock:
            previous=self._sessions.get(key)
            if previous and previous['until']<=time.time():previous=None
            source=self.private_source(interaction)
            kind=route['kind']
            if kind=='reuse' or kind=='source' and previous and previous['message_id']!=source.id:
                editor=previous['edit'] if previous else None
                deleter=previous['delete'] if previous else None
                until=previous['until'] if previous else time.time()+840
            else:
                editor=interaction.edit_original_response
                deleter=interaction.delete_original_response
                until=time.time()+840
            kwargs={'content':content}
            if not preserve:
                kwargs.update(embed=embed,view=view,attachments=attachments or [])
            elif kind=='original' or previous is None and source is None:
                kwargs.update(embed=embed,view=view,attachments=[])
            message=None
            if editor is not None:
                try:message=await editor(**kwargs)
                except (discord.NotFound,discord.Forbidden):
                    # Replace only an unavailable private workspace, never a
                    # public launcher or an arbitrary failed response.
                    if kind=='original':raise
            if message is None:
                message=await interaction.followup.send(content=content,embed=embed,view=view,
                                                        files=attachments or [],ephemeral=True,wait=True)
                editor,deleter=message.edit,message.delete;until=time.time()+840
            self._sessions[key]={'message_id':message.id,'edit':editor,'delete':deleter,'until':until,
                                 'idle_until':time.time()+600}
            self._routes.pop(interaction.id,None)
            if previous and previous['message_id']!=message.id:
                await self._retire(previous['delete'])
            if source and source.id!=message.id and (not previous or source.id!=previous['message_id']):
                await self._retire(interaction.delete_original_response)
            return message

    @staticmethod
    async def _retire(deleter):
        try:await deleter()
        except Exception:
            logging.getLogger(__name__).info('An older private panel could not be retired.')

    async def error(self,interaction,content,*,view=None):
        if not interaction.response.is_done():await self.defer(interaction)
        await self.render(interaction,content=content,view=view,preserve=True)

    async def expire_idle(self,*,now=None):
        """Retire private UI before its webhook expires; never delete shared messages."""
        now=time.time() if now is None else now
        for key,session in list(self._sessions.items()):
            if min(session['until'],session['idle_until'])>now:continue
            async with self._locks.setdefault(key,asyncio.Lock()):
                current=self._sessions.get(key)
                if current is not session:continue
                self._sessions.pop(key,None)
                await self._retire(session['delete'])
