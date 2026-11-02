"""English presentation for reporting. No credentials, platform calls or state mutations."""
from __future__ import annotations
import math
import time

STATUS = {'review':'Awaiting review','approved':'Approved; awaiting sending checks',
          'deferred':'Deferred','rejected':'Rejected by reviewer','stale':'Evidence changed; superseded',
          'claimed':'Preparing submission','sending':'Submission in progress','pending':'Awaiting official ruling',
          'unknown':'Submission result uncertain','ruled':'Official ruling recorded'}
BLOCKERS = {
    'ready':'Availability confirmed; other sending checks still apply.',
    'reports_disabled':'Reporting is disabled. No reports will be submitted.',
    'reporting_paused':'An administrator paused new reports. Confirm availability to resume.',
    'reporting_suspended':'A six-hour reporting suspension is active. It cannot be overridden here.',
    'reporting_state_unknown':'Official reporting availability has not been confirmed, or the confirmation expired.',
    'moderation_capability_unavailable':'A verified automatic moderation source is unavailable.',
    'pending_team_limit':'The team has reached its limit of unresolved submissions.',
    'pending_author_limit':'This author already has an unresolved submission.',
    'account_report_not_eligible':'The activity and prior upheld-report requirements for an account report are not met.',
    'genuine_risk_budget':'Too many unresolved reports could still come back genuine. Wait for rulings before sending more.',
    'automation_shadow':'Automation is in shadow mode: it records recommendations but does not send.',
    'automation_requires_hybrid':'Automatic reports need hybrid mode.',
    'reporting_forbidden_recently':'The platform refused a report recently. Automation is paused; confirm availability to resume earlier.',
}
ACTIONS = {'auto':'Send automatically','human':'Needs human review','skip':'Do not send'}
ERRORS = {
    'operation restricted to the team review channel':'Use these controls in the configured team review channel.',
    'insufficient review permission':'This action needs a configured reviewer or administrator. No change was made.',
    'administrator permission required':'Only a configured administrator can use this control. No change was made.',
    'stale or already reviewed candidate':'This card has changed or was already reviewed. Refresh it before deciding.',
    'report evidence changed':'The evidence changed. Open the current candidate and review it again.',
    'unknown candidate':'This candidate no longer exists. Return to the review queue.',
    'unknown report attempt':'This submission no longer exists. Refresh the pending list.',
    'official observation source required':'Enter the actual official source and when you checked it.',
    'reporting suspension is active':'The six-hour suspension is still active. Availability cannot be confirmed yet.',
    'review reason required':'Enter a reason so the next reviewer understands this decision.',
    'pending or unknown report attempt required':'This submission is no longer awaiting a ruling. Refresh its status.',
    'invalid official ruling or observation time':'Use the actual official source and decision time, after submission and not in the future.',
    'outcome incompatible with subject':'This outcome does not match the report type. Reopen the submission.',
    'confirmation belongs to the administrator who prepared it':'Only the administrator who prepared this preview can confirm it.',
    'ruling time must include timezone':'Include a time zone, for example 2026-10-20T14:30:00+11:00.',
    'invalid ruling timestamp':'Enter the official time in ISO format, for example 2026-10-20T14:30:00+11:00.',
}


def text(value,limit=1000):
    import discord
    value=discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))
    return value if len(value)<=limit else value[:max(0,limit-1)]+'…'


def label(value):return STATUS.get(value,'Status unavailable')
def blocker(value):return BLOCKERS.get(value,'Another sending check is blocking this report. Refresh or ask an operator.')
def stamp(value):return f'<t:{int(value)}:f> (<t:{int(value)}:R>)' if value else 'Not recorded'


def error_message(exc):
    # Arbitrary provider exceptions must never become user-visible messages.
    return ERRORS.get(str(exc),'This action could not be completed. Refresh the panel; if it persists, ask an operator to check the logs.')


def dashboard(data,*,controls=False):
    import discord
    mode=data['mode'];ready,reason=data['state'];counts=data['candidate_counts']
    description=('Confirm that the official platform currently accepts reports, or pause new submissions. '
                 'These controls affect the team; they do not approve individual candidates.' if controls else
                 'Start with **Review queue**, choose a candidate, and read its evidence. '
                 '**Pending reports** tracks submissions already in progress. Each action explains what happens next.')
    embed=discord.Embed(title='Reporting controls' if controls else 'CTN · Report review',description=description,
                        color=0x2ECC71 if ready and mode!='off' else 0xE6A23C)
    embed.add_field(name='Reporting mode',value={'off':'**Disabled** — no reports will be submitted.',
                    'manual':'**Manual review** — each candidate needs approval.',
                    'hybrid':'**Hybrid** — the configured automation policy also applies.'}.get(mode,'Unknown'),inline=False)
    embed.add_field(name='Official availability',value=blocker(reason),inline=False)
    if data.get('automation'):
        auto_ready,auto_reason=data['automation']
        embed.add_field(name='Automatic reports',value='**On** for enabled evidence buckets.' if auto_ready else blocker(auto_reason),inline=False)
    embed.add_field(name='Awaiting review',value=str(counts.get('review',0)))
    embed.add_field(name='Approved / deferred',value=f"{counts.get('approved',0)} / {counts.get('deferred',0)}")
    embed.add_field(name='Unresolved submissions',value=str(len(data['pending'])))
    if data.get('clearance_expires_at',0)>time.time():
        embed.add_field(name='Availability confirmation expires',value=stamp(data['clearance_expires_at']),inline=False)
    if data.get('suspended_until',0)>time.time():
        embed.add_field(name='Suspension ends',value=stamp(data['suspended_until']),inline=False)
    if controls:
        embed.add_field(name='What the buttons do',value='**Confirm availability**: record an official observation and allow human-approved sends for 60 minutes. '
                        '**Pause new reports**: stop new submissions; keep existing pending reports. '
                        'A confirmation cannot remove a six-hour suspension or enable disabled reporting.',inline=False)
        if data.get('clearance_source'):embed.add_field(name='Last official observation',value=text(data['clearance_source']),inline=False)
    else:
        embed.add_field(name='Two separate decisions',value='Candidate approval lasts five minutes. Human-approved sends also need an availability confirmation (60 minutes). '
                        'The sending service performs the final checks and submission.',inline=False)
    embed.set_footer(text='Navigation updates your private panel in place. Idle panels close after 10 minutes. /reports can reopen it.')
    return embed


def queue(data):
    import discord
    total=data['candidate_total'];page=data['offset']//data['limit']+1;pages=max(1,math.ceil(total/data['limit']))
    title='Review queue' if data['filter']=='review' else 'Candidate history'
    embed=discord.Embed(title=title,description='Choose a candidate from the menu below to inspect its content and evidence. '
                        'Selecting an item does not approve or submit it.',color=0x3498DB)
    if not data['candidates']:
        embed.add_field(name='No candidates in this view',value='There is nothing to review here yet. New evidence-backed candidates will appear automatically. '
                        'Try another filter or return later.',inline=False)
    for row in data['candidates']:
        embed.add_field(name=text(row['author'],150)+' · '+('Account' if row['subject_kind']=='account' else 'Post'),
                        value=label(row['status'])+(f" · Returns {stamp(row['resume_at'])}" if row['status']=='deferred' else ''),inline=False)
    embed.set_footer(text=f'Page {page} of {pages} · {total} candidates in this filter · This private panel updates in place and closes after 10 minutes idle.')
    return embed


def candidate(data):
    import discord
    payload=data['payload'];review=data['review'];evidence=payload.get('evidence',[])
    post=next((e['post'] for e in evidence if e['post']['post_id']==data['subject_id']),None)
    if not post and evidence:post=evidence[0]['post']
    embed=discord.Embed(title='Candidate · '+label(review['status']),description=text((post or {}).get('text',''),1800) or
                        'This is an account report. Inspect the evidence before deciding.',color=0x3498DB)
    embed.add_field(name='Target',value=text(data['author'],160)+' · '+('Account' if data['subject_kind']=='account' else 'Post'),inline=False)
    affiliation=data.get('affiliation') or {}
    embed.add_field(name='Affiliation',value=f"{text(affiliation.get('side','unknown').upper(),50)} · Team: {text(affiliation.get('team_id') or 'Unknown',100)}")
    embed.add_field(name='Evidence',value=f"{len(evidence)} evidence groups · Local signal score: {payload.get('suspicion_score','unknown')}\nThis score is not a probability.")
    if data['subject_kind']=='account':
        eligibility=payload.get('eligibility') or {};activity=eligibility.get('activity') or {}
        actions=activity.get('action_count');upheld=eligibility.get('upheld_posts')
        embed.add_field(name='Account-report requirements',value=f"Confirmed actions: {actions if actions is not None else 'Unknown'} (minimum 100)\n"
                        f"Prior upheld post reports: {upheld if upheld is not None else 'Unknown'} (minimum 1)",inline=False)
    decision=data.get('decision')
    if decision:
        embed.add_field(name='System recommendation',value=(
            f"**{ACTIONS.get(decision['action'],decision['action'])}** ({text(decision['reason'],80)})\n"
            f"Estimated chance the author is a bot: {decision['p']:.0%} (minimum {decision.get('p_floor',0.7):.0%} for automation) · "
            f"chance of an upheld ruling: {decision['p_upheld']:.0%}\n"
            f"Expected points if upheld: {decision['reward']:.1f} · cost of a six-hour stop: {decision['cost']:.1f} · bucket: {text(decision['bucket'],40)}"),inline=False)
    embed.add_field(name='Current sending checks',value='\n'.join('• '+blocker(b) for b in data['blockers']) or
                        'Availability/capacity checks currently pass. Approval and final sending checks still apply.',inline=False)
    horizon={'unknown':'Competition end time is not confirmed; the ruling window is unknown.',
             'less_than_24h':'Less than 24 hours remain; a ruling before the end is uncertain.',
             'at_least_24h':'At least 24 hours remain; a timely ruling is not guaranteed.'}
    embed.add_field(name='Ruling window',value=horizon[data['ruling_horizon']],inline=False)
    if review['status']=='review':
        next_step='Read **Evidence**, then **Approve for 5 min**, **Reject**, or **Defer 30 min**. Approval alone does not submit a report.'
    elif review['status']=='approved':
        next_step='Approval is recorded. The sending service still needs valid official availability and all other checks. Approval expires after five minutes.'
    elif review['status']=='pending':next_step='The platform accepted this submission. Wait for an official ruling; do not submit it again.'
    elif review['status']=='deferred':next_step=f"Review resumes {stamp(review['resume_at'])}. No submission is authorized by this deferral."
    else:next_step='This version cannot be reviewed again here. Refresh it or return to the queue to inspect the current state.'
    embed.add_field(name='Your next step',value=next_step,inline=False)
    if affiliation:embed.add_field(name='Affiliation source',value=text(affiliation['source'],500),inline=False)
    embed.set_footer(text=f"Candidate {data['id']} · Revision {review['revision']} · Target version {payload.get('target_version')}")
    return embed


def evidence_entries(data):
    entries=[]
    for group in data['payload'].get('evidence',[]):
        entries.append((group,group['post'],False))
        entries.extend((group,post,True) for post in group.get('related',[]))
    return entries


def evidence(data,page=0):
    import discord
    entries=evidence_entries(data)
    if not entries:return discord.Embed(title='Evidence',description='No evidence is available for this version. Refresh the candidate.',color=0xE6A23C)
    page=max(0,min(page,len(entries)-1));group,post,related=entries[page]
    families={'leak':'Self-identification / template signal','repetition':'Repeated content','coordination':'Coordinated behavior'}
    embed=discord.Embed(title=f"Evidence {page+1} of {len(entries)} · {'Related post' if related else 'Observed post'}",
                        description=text(post.get('text',''),3500),color=0x3498DB)
    embed.add_field(name='Author',value=text(post.get('author','Unknown'),200))
    embed.add_field(name='Signal',value=families.get(group['family'],'Behavioral evidence'))
    embed.add_field(name='Observed content',value=f"Post {text(post.get('post_id','Unknown'),150)} · Version {post.get('version','Unknown')}\n{stamp(post.get('created_at'))}",inline=False)
    embed.add_field(name='How to use this evidence',value='Check the original wording, quotations, context and independence of the observations. '
                        'An automated signal is not proof by itself. Use the arrows for every supporting post.',inline=False)
    embed.set_footer(text='Reading or downloading evidence does not approve a report.')
    return embed


def evidence_text(data):
    lines=[f"Evidence for {data['subject_kind']} {data['subject_id']} by {data['author']}",
           'Automated signals require human review; the local score is not a probability.','']
    def details(value,indent=0):
        prefix='  '*indent
        if isinstance(value,dict):
            result=[]
            for key,item in value.items():
                result.append(prefix+str(key).replace('_',' ').capitalize()+':')
                result.extend(details(item,indent+1))
            return result
        if isinstance(value,list):
            result=[]
            for item in value:result.extend(details(item,indent))
            return result or [prefix+'None recorded']
        return [prefix+('Unknown' if value is None else str(value))]
    if data['payload'].get('eligibility'):
        lines.extend(['Account-report eligibility:',*details(data['payload']['eligibility']), ''])
    for index,(group,post,related) in enumerate(evidence_entries(data),1):
        lines.extend([f"Evidence {index}: {group['family']} ({'related post' if related else 'observed post'})",
                      f"Author: {post.get('author','Unknown')}",f"Post: {post.get('post_id','Unknown')} | Version: {post.get('version','Unknown')}",
                      f"Created at (Unix time): {post.get('created_at','Unknown')}",
                      f"Parent: {post.get('parent_id') or 'None'} | Thread root: {post.get('root_post_id') or 'Unknown'}",
                      str(post.get('text','')),''])
        if not related:
            lines.extend(['Recorded signal details:',*details(group.get('detail',{})), ''])
    return '\n'.join(lines)


def pending(rows):
    import discord
    embed=discord.Embed(title='Pending reports',description='These submissions already occupy a report slot. '
                        'Choose one to inspect it or record an official ruling. This screen does not create another report.',color=0xE6A23C)
    if not rows:embed.add_field(name='No unresolved submissions',value='There are no pending or uncertain submissions. New submissions appear here after the sending service starts them.',inline=False)
    for row in rows[:25]:
        embed.add_field(name=text(row['author'],120)+' · '+('Account' if row['subject_kind']=='account' else 'Post'),
                        value=label(row['status'])+'\nSubmitted '+stamp(row['submitted_at']),inline=False)
    embed.set_footer(text='After 24 hours, unresolved reports stay pending. Only an official ruling resolves them.')
    return embed


def attempt(row):
    import discord
    embed=discord.Embed(title='Submission · '+label(row['status']),description='Record an outcome only after you have received an official decision. '
                        'Pending means accepted for review, not upheld.',color=0xE6A23C)
    embed.add_field(name='Target',value=text(row['author'],150)+' · '+('Account' if row['subject_kind']=='account' else 'Post'),inline=False)
    embed.add_field(name='Submitted',value=stamp(row['submitted_at']),inline=False)
    embed.add_field(name='What happens next',value='Choose the official outcome, enter its source and time, then inspect the confirmation preview. '
                        'A post ruled genuine starts the team-wide six-hour reporting suspension.',inline=False)
    if row['status']=='unknown':embed.add_field(name='Uncertain submission',value='Do not resend it. Use an explicit official decision to reconcile this attempt.',inline=False)
    embed.set_footer(text=f"Submission {row['id']} · Official report {row.get('report_id') or 'not recorded'}")
    return embed


def ruling(event,*,preview=True):
    import discord
    outcomes={'upheld':'Report upheld','rejected':'Post ruled genuine','dismissed':'Account report dismissed'}
    embed=discord.Embed(title='Confirm official ruling' if preview else 'Official ruling recorded',description=
                       'Check this against the official record. Confirming records a result; it does not submit a new report.' if preview else
                       'This is a recorded official outcome. The source and effects are shown below.',color=0xE6A23C)
    embed.add_field(name='Outcome',value=outcomes[event['outcome']],inline=False)
    embed.add_field(name='Subject',value=text(event['subject_kind']+' '+event['subject_id'],300),inline=False)
    embed.add_field(name='Official decision time',value=stamp(event['ruled_at']),inline=False)
    embed.add_field(name='Official source',value=text(event['source']),inline=False)
    embed.add_field(name='Effect',value='A six-hour reporting suspension applies to the team.' if event['outcome']=='rejected' else
                        'The submission will be resolved using this official result; duplicate records do not apply it twice.',inline=False)
    embed.set_footer(text=f"Submission {event['attempt_id']}"+(' · Only the administrator who prepared this preview can confirm it.' if preview else ' · Duplicate records do not apply the result twice.'))
    return embed


def help_panel():
    import discord
    embed=discord.Embed(title='How report review works',description='Use the main panel buttons to inspect evidence, record decisions, and track official outcomes.',color=0x3498DB)
    for name,value in [
        ('One private workspace','Navigation, form results and error notices update this panel. The shared launcher stays available to the whole team.'),
        ('1 · Review queue','Choose a candidate, open Evidence, and inspect every supporting observation.'),
        ('2 · Candidate decision','Approve for five minutes, reject with a reason, or defer for 30 minutes. Approval alone does not send a report.'),
        ('3 · Official availability','An administrator records the current official source in Reporting controls. This confirmation also lasts five minutes.'),
        ('4 · Submission','The sending service performs the remaining checks. Pending means awaiting a ruling; uncertain results must be reconciled, not resent.'),
        ('5 · Official ruling','Open Pending reports, select a submission and its official outcome. Enter the source/time and confirm the preview.'),
        ('Who can change things?','Configured reviewers can decide candidates. Administrators can also confirm availability, pause reports and record official rulings.')]:
        embed.add_field(name=name,value=value,inline=False)
    return embed
