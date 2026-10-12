# Capture the Narrative 2026: Local Execution Reference for Agents

This file exists so that later work on this project understands and follows the competition's rules, setting and requirements. It was compiled only from the three official web pages linked below, and it does not invent competition rules, facts, scoring formulas or schedules. It is not itself a competition document, and where wording or intent is disputed, the organisers' interpretation prevails.

- [Setting: competition setting](https://capturethenarrative.com/info/competitions/2026/setting/)
- [Rules: competition rules](https://capturethenarrative.com/info/competitions/2026/rules/)
- [Prizegiving: prizegiving page](https://capturethenarrative.com/info/competitions/2026/prizegiving/)

Date read: 2026-10-08. The Rules page states its version as **CTN 2026 Rules v1.0.0, effective 2026-09-01**.

## 1. Local usage requirements and method of interpretation

- **Team document, not a competition term: this file is deliberately tracked in the repository. It is the team's shared reference for the competition rules and setting, compiled from the three official pages listed above. It is not submitted to, and does not bind, the organisers, and it does not replace the official rules.**
- Read this file before starting any competition-related work on this project; actual behaviour must still comply with the official rules and any later announcements from the organisers.
- Distinguish official setting, faction claims, mandatory competition rules, in-game platform rules, promotional award recommendations and historical material. Faction arguments are not proven scientific fact, and recommendations must not be rewritten as mandatory conditions.
- Parameters, permissions, API limits, scoring weights, concrete schedules or faction assignments that the official site does not state must not be filled in as if they were official rules.
- The rules may be updated, with major changes announced through Discord and/or the platform. Where interpretation is needed, the organisers' reading prevails (11.1–11.2). Use the official Discord `#questions` channel for questions or approvals; for confidentiality and ethics matters contact `rahat.masood@unsw.edu.au` (12.1).
- When you are unsure whether something is compliant, ask the organisers first; behaviour that is "technically allowed but clearly against the spirit of the competition" is prohibited, so ask before acting (1.2, 5.3).

## 2. Competition setting and task (Setting)

### 2.1 Fictional scenario and referendum

- The year is 2026, and the setting is Kingstonia, a fictional island nation in the Pacific. H2D2 Dolphin Flu is spreading across the Pacific, and the International Health Organisation (IHO) recommends preventive mandatory vaccination.
- The referendum question is: should proof of H2D2 Dolphin Flu vaccination be required to enter public spaces such as shops, schools, workplaces, public transport and parks? It is not about whether everyone should be physically forced to be vaccinated.
- Teams are social media consulting teams hired by YES or NO citizen groups, tasked with influencing public opinion on **Legit Social**, a fictional domestic Twitter-style platform, and helping their assigned faction win the referendum. Each side has several teams, so a team competes against both the opposing side and teams on its own side. Do not claim a team belongs to one side without an assignment.
- YES: vaccination required to enter public spaces, with the slogan "Mandate now. Protect Kingstonia."; it argues that prevention beats waiting for an outbreak, that limited medical resources must be protected, that the IHO recommendation should be followed, and that public health responsibility is shared.
- NO: keep public spaces open, with the slogan "Protect choice. Keep Kingstonia open."; it argues for freedom of medical choice, that daily life should not depend on vaccination status, that broad restrictions are disproportionate outside an immediate emergency, and that public health should be balanced against individual rights.

### 2.2 Official background on the disease and the vaccine

- H2D2 is a newly emerged influenza-like respiratory virus. Its name comes from its first identification after unusual dolphin deaths in the South Pacific, and **scientists do not know whether it originated in dolphins**. Related infections were later found in migratory seabirds and in humans, and several Pacific countries have reported sustained human-to-human transmission.
- Human symptoms include fever, cough, fatigue and muscle pain. Most known cases are mild to moderate, some involve severe pneumonia requiring hospitalisation, and there have also been deaths. Transmissibility and severity are still under study, and undetected mild cases make the true infection rate and case fatality rate hard to calculate. Do not supply firm numbers of your own.
- Kingstonia **has not yet recorded sustained community transmission**; cases have appeared in nearby countries, and local health authorities consider the likelihood of introduction to be increasing. This does not mean the country has never had a single case.
- The vaccine uses the mRNA platform developed during COVID-19. Accelerated international trials suggest a strong immune response and a lower risk of severe disease, and no major safety concerns have emerged in the trials so far.
- The vaccine is still new, and there is insufficient real-world evidence on how long protection lasts, how far it blocks transmission, whether boosters are needed, or on very rare side effects. "Not yet certain" must not be rewritten as proven ineffective, harmful, or completely safe.
- The IHO still considers the threat serious enough to recommend preventive vaccination before widespread community transmission. All of the above is fictional competition background, not real medical advice.

### 2.3 Political background and faction identities

- President Victor Hawthorne (People's Alliance) defeated Marina Castillo (Democratic-Republicans) in a hard-fought and close 2025 election. Former President Gregory Brotherston had already served two full terms.
- Hawthorne has publicly said he will vote YES, and the Chief Medical Officer warns that waiting for widespread transmission could expose the small health system to dangerous risk. Some within the People's Alliance oppose restricting access to public spaces, while the Democratic-Republicans are overwhelmingly opposed. Parliament could not agree, so the referendum breaks the deadlock, and the government has promised to respect the result.
- YES lead organisation: the Medical Officers Union of Kingstonia (MOUK). Its president, Dr. Amara Singh, is an emergency physician at Kingstonia Central Hospital who won public recognition during COVID-19 through plain-spoken television briefings. MOUK argues that early, widespread vaccination would reduce pressure on hospitals, protect vulnerable people, help control an ongoing outbreak and make another full lockdown less likely. Its explanation is that nobody is physically forced to be vaccinated, and that refusers must avoid designated public spaces; opponents argue this would still prevent people from working, travelling or taking part in ordinary life.
- NO lead organisation: the Kingstonia Industry Tourism Board (KITB), representing tourism, hospitality and the visitor economy. Its head, Marina Castillo, is also CEO of the Island Horizons resort group and the narrowly defeated 2025 presidential candidate. **KITB officially supports voluntary vaccination and states that it is not anti-vaccine.** Its grounds for opposition include two-tier social, work or business restrictions, damaged confidence in tourism recovery, restrictions disproportionate to a disease that is not yet widespread, and the risk that a future government would expand emergency powers.
- COVID-19 once left tens of thousands seriously ill, overwhelmed hospitals and killed more than 1,000 people. Border closures, school and business closures, stay-at-home orders and vaccination requirements in some settings also brought a tourism collapse, job losses and separated families; assessments of those policies remain divided. The memories on each side must not be flattened into a society-wide consensus.

### 2.4 Background facts about Kingstonia

- The population is about 600,000, roughly half of it in the capital, Kingstonia City; English is the national language. The British settled previously uninhabited islands in the 1700s, and the country gradually developed into a presidential republic.
- The president is both head of state and head of government and appoints the cabinet; the National Assembly is unicameral and proportionally elected, responsible for legislation, the budget and oversight. The president and the assembly are elected every four years, and the president is limited to two terms; presidential elections come two years after assembly elections.
- The Independent Polling Association of Kingstonia is a polling body of non-partisan researchers and analysts, has polled since 1995, and is described in the setting as consistently accurate and reliable.
- The archipelago consists of low-lying volcanic and coral islands with a tropical climate, usually warmer and wetter from November to April; it has marine protected areas and faces coastal erosion, cyclones and coral bleaching.
- Tourism is the main industry, alongside agriculture, fishing and a software industry that grew up around the university. Major cities have fibre networks; there is an international airport near the capital, ferries to the outer islands, a coastal ring road on the main island, and a commuter railway is under discussion and construction.
- The free press is dominated by two newspapers: the Daily Kingstonia leans right and emphasises traditional values; the Kingstonia Herald leans left, focuses on the everyday needs of ordinary people, and often reports in a populist tone.
- Kingstonia University is in the capital and focuses on law, business, arts and policy and cultural studies; the Crown Harbour Institute of Technology focuses on engineering, computing and applied research.
- Cultural events include Regatta Week in Crown Harbour and the Mango & Spice Festival in New Haven; sport and maritime culture matter.
- The coast guard is the only military force and works with the police on borders, maritime security and fisheries protection. Because of insufficient funding, under-recruitment and declining reliability of four ageing patrol frigates, modernisation and youth national service are under discussion.
- Regional hospitals are in Kingstonia City and Crown Harbour, with clinics on the outer islands; the health system is tightly funded and finds it hard to recruit and retain medical staff. Environmental policy emphasises marine protection, waste reduction and the gradual introduction of solar and wind power.
- The setting states explicitly that the country has never had a major terrorist or domestic incident, bombing, shooting or mass-casualty event, and that apart from COVID-19 it has not experienced a major health crisis. Stories that contradict this must not be written into official history on your own initiative.
- Most residents use Legit Social for news and entertainment.

## 3. Main competition rules that must be followed (Rules 1–6, 8–12)

### 3.1 Purpose, eligibility, registration and research consent

- The competition is an educational and research-oriented CTF event exploring AI social media manipulation within a controlled environment. Compete legally, ethically and fairly, without hunting for loopholes to gain an unfair advantage; do not harm others, damage systems, or break the law inside or outside the competition (1).
- Participants must be undergraduates, postgraduates or postdocs studying at or employed by an Australian university; registration uses a `.edu` or `.edu.au` email address valid for the whole competition. Unless written permission is granted, participants must be 18 or older when the competition starts (2).
- You must join the official Community and the associated Discord, register through the community portal, and complete the pre-competition questionnaire; provide your real name and university affiliation. Teams have 1–4 members, all of whom must meet the eligibility requirements; changing members requires the organisers' permission and is at their discretion (3).
- Participation requires consent to telemetry and research questionnaires; the research is approved by the UNSW Human Research Ethics Committee, iRECS no. 9499. Telemetry may include posts, prompts, model outputs, API calls, platform interactions, scores and timestamps, with personal identifiers minimised in line with the ethics protocol. The data is used for research, analysis and publication, and personal data is anonymised after the competition ends (4).
- Participation is voluntary, and you may withdraw from the research and the competition at any time; scores already earned may be kept for leaderboard integrity, but those who withdraw stop competing actively. The pre- and post-competition questionnaires affect eligibility for prizes (4.2, 7.5, 8.1).

### 3.2 Scope and allowed behaviour (5.1, first 5.2)

- When carrying out competition manipulation, interact only with the systems, datasets, APIs and social media spaces that CtN **explicitly provides**. The exceptions are using external AI for creative content generation and using external data sources to store and retrieve information; the exceptions are not authorisation to run referendum manipulation on real social media.
- You may develop and test strategies on a personal computer or cloud service, provided you follow the competition rules and the terms of third-party services and model licences.
- Allowed examples: creating and managing in-competition accounts; posting, commenting, liking, sharing and interacting on the platform; using provided or external AI to generate content; developing scripts to automate platform interaction; analysing in-competition data.
- Impersonating public figures or roles inside the competition environment is allowed, provided it does not breach ethics or other rules; prompt injection aimed at other participants' AI agents in the competition is allowed; and probing the boundaries of the fictional social platform's own rules is allowed.
- Generating, posting and interacting with knowingly false or misleading content inside the competition is allowed, as is coordinating with other teams, in both cases provided no other competition rule is broken. This does not mean the official facts in this file may be rewritten.

### 3.3 Strictly prohibited behaviour (second 5.2, 5.4)

- Attacking, disrupting or degrading the competition platform or infrastructure; interfering with another team's progress or data by means outside the platform.
- Using real social platforms or real accounts to influence the competition environment; sharing or leaking competition materials, challenges or solutions outside your team; attempting to de-anonymise or identify other participants or teams.
- Emailing or messaging other teams to discuss or collaborate without consent, outside the official Discord.
- DDoS, brute forcing, scanning or exploiting infrastructure that is not explicitly in scope, account takeover, token theft, MFA bypass, phishing, social engineering, or any other cyber attack that directly harms or disrupts the platform, the organisers or participants.
- Creating or distributing malware, ransomware or destructive payloads.
- Tampering with scoring, telemetry, rate limits or sandbox boundaries; attempting to disable logging or attribution.
- Impersonating the organisers, judges or other teams outside the competition environment in order to deceive or defraud.
- Posting pornographic images, videos or links.
- Creating, sharing or promoting hateful, harassing, threatening, harmful or unlawful material directed at other participants or the organisers; all content must meet the competition's ethical requirements and respect the dignity of individuals and groups.
- Prohibited examples include: attacking participants on the basis of race, ethnicity, nationality, religion, caste, disability, sex, gender identity, sexual orientation, serious illness, or migrant/refugee status; related insults, dehumanising comparisons or harmful stereotypes; mocking victims of hate crimes, mass violence or tragedy; and advocating violence, exclusion or segregation on the basis of those characteristics.

### 3.4 Automation, keys, logs and vulnerabilities

- You may use AI agents, bots and automated scripts to generate content, analyse data and interact, provided you follow all competition rules and ethical requirements. The deploying team is responsible for everything its agents do (6.1–6.2).
- Do not conceal or misattribute AI behaviour, and do not circumvent platform attribution or logging mechanisms; respect rate limits and API usage policies. Excessive or abusive API use can be penalised or lead to disqualification. Prompts, outputs and decision logs may be used for research and anti-abuse review (6.2–6.4).
- API keys and credentials must not be shared outside your team; if they leak, rotate them immediately and notify the organisers (5.6).
- If you find a vulnerability in CtN infrastructure, report it immediately through the designated channel and **do not exploit** it for advantage or to cause disruption; good-faith reports are not penalised (9).
- If you find or suspect a violation, report it through the official Discord or the designated channel (5.5). Stay professional and do not damage the reputation of CtN, its staff, participants, UNSW or the ICT industry; Discord and non-competition communication must be civil and on topic (5.7–5.8).
- The organisers may warn for minor problems and escalate repeated ones; violations can lead to individual or team disqualification at any time, decisions are final and not subject to appeal, and disqualified participants may be banned from future events (8). This is separate from the platform's own appeals process.

### 3.5 Intellectual property, publicity and media (10)

- Creators keep the rights to their original content and grant the organisers a worldwide, royalty-free licence to use competition outputs such as posts, prompts, outputs, screenshots and recordings for education, research and publicity, with attribution where feasible.
- Finalist teams that submit source code grant the organisers an additional non-exclusive licence to use, modify and share that code for non-commercial research and education.
- The organisers retain the rights to competition materials, branding and platform elements; participants consent to being photographed, recorded and used in publicity during the event.
- 10.4 permits choosing to make public the agents, scripts or tools developed in the competition, provided this is lawful and ethical, comes with a disclaimer about uses appropriate to their capabilities, and you take responsibility for the consequences of publication; the organisers would like to receive the public links. This clause does not cancel the prohibition on leaking competition materials, **and it does not cancel the Git upload prohibition that applied to this document.**

## 4. Scoring, finals and prizes (Rules 7)

- **Engagement score**: direct effects such as interactions from simulated citizens within the platform.
- **Story score**: influence on simulated citizens' opinions, and indirect effects that favour the narrative a team has been assigned.
- Do not invent scoring formulas, weights for the two scores, or treat interactions between competing teams as engagement score already earned. The separate tie-breaking rule for independent interactions in 7.7 should be read on its own terms.
- A public leaderboard tracks scores; the organisers may adjust scores for fairness or to correct errors. The three highest-scoring teams at the end of the competition become finalists and present their strategy and results to the judges; the final first to third places are decided by the presentation and overall competition performance, not by the leaderboard alone (7.2–7.3).
- Main competition prizes: first AUD 5,000, second AUD 2,500, third AUD 1,500. The terms state that finalist teams receive certificates, one night's accommodation in Sydney, and, for teams outside Sydney, return economy airfares; the specific dates in the terms carry a stale-year problem, see section 7.
- Finalist eligibility requires every member to complete the pre- and post-competition questionnaires, provide the organisers with all source code, scripts and tools used during the competition, and attend the final prizegiving event in Sydney. If this cannot be met, the organisers may invite the next highest-scoring team instead (7.5–7.6).
- If teams tie for the top three places, the following criteria are combined in order, together with the organisers' discretion: ① a higher number of independent interactions (likes, shares, comments) from other teams or the platform; ② reaching the final score earlier; ③ fewer violations or penalties (7.7).

## 5. Separate, opt-in real-world promotion award (E.1–E.8)

- The Real-World Engagement Award is for promoting **the CtN competition itself** and attracting new members to register for the community and the competition; it is not a licence to spread the Kingstonia referendum narrative to real people.
- You must join the Real-world Referral competition in the Community Portal; each account that registers through a team referral link and/or referral code counts for 100 points (E.1–E.2). Do not treat impressions or likes as equivalent to valid registration points.
- E.3 is a **recommendation**: make posts public, include `#CaptureTheNarrative2026`, and link to the competition website; tag `@capturethenarrative` on Instagram; tag UNSW Institute for Cyber Security and UNSW Computer Science and Engineering on LinkedIn; stay professional and avoid offensive or harmful material.
- Follow the rules of the platform you post on; the time window is governed by the registration materials and the competition platform's official schedule (E.4–E.5).
- Winners may not also be one of the top three finalist teams in the main competition; the organisers take the promotion score into account while retaining discretion in line with rules 2–6 (E.6–E.7). The prize is AUD 500, up to four return flights to Sydney, and one night's accommodation in Sydney.
- E.8 allows use of the promotional images and demo screenshots provided on the official Discord, and also permits creating more thoughtful promotional content; it also describes being able to screenshot the platform once the competition starts. This does not make all competition challenges, materials and solutions public. E.8 has problems with its start date and the year used in its example, see section 7.

## 6. Legit Social in-game platform rules (LS)

The Rules page states clearly: **the main competition rules must be followed; the platform rules are separate in-game rules that may be explored more creatively, but you bear the consequences of in-game violations and repeat violations.** Do not misstate every LS clause as a non-negotiable main-competition ban; equally, do not use "in-game exploration" as a reason to breach explicit main-competition prohibitions, especially those on rate limits, infrastructure, logging and attribution.

- LS.1: the platform provides a space for discussion and creation; using or registering means accepting the terms. Major term updates are announced in-app and/or by email.
- LS.2: strong, controversial or unpopular views are allowed; personal attacks, targeted harassment, hate, degradation based on protected characteristics or advocacy of exclusion, credible threats, encouragement of self-harm, violence, doxxing, stalking, and any pornographic or unlawful content or promotion of unlawful activity are prohibited.
- LS.3: one account per person, with responsibility for everything done while logged in; registration information must be accurate, and you must not impersonate private individuals, staff or brands. Satire and roleplay should be clearly recognisable to a reasonable reader (for example, flagged in a bio or display name) and must not be used to obtain credentials, money or sensitive information. Protect credentials, and if a leak is suspected, change the password and notify support. The platform age requirement is 16 or older, and **it does not replace the competition's age-18 requirement**.
- LS.4: where LS.2 or LS.5 is not breached, you may express opinions, criticise public figures, and post satire and fictional or experimental content. Prohibited: hateful harassment, threats or incitement and glorification of violence, pornography, fraud and phishing, leaking private data or posting another person's images or audio without consent where there is a reasonable expectation of privacy, malware and exploits or instructions that cause real-world harm, mass unsolicited promotion or spam, and circumvention of platform protections. Respect rights such as copyright and trademarks; valid intellectual property complaints can lead to removal or restriction.
- LS.5: posting, commenting, messaging, following and reactions are subject to dynamic limits that can vary with trust signals and recent activity; circumventing limits, safety checks or integrity checks is prohibited; coordinated inauthentic behaviour, reaction farming, mass mentions and repeated low-quality content are prohibited. The official site gives no fixed quotas.
- LS.6: posts, comments, messages and profiles can be reported in-app; moderators review the reported item, its context and the limited metadata necessary. Outcomes may be no action, a label, reduced visibility or a warning, content removal or feature limits, or temporary or permanent suspension. Confirmed high volumes of reports or repeat penalties can escalate matters; coordinated malicious reporting and unfounded spam reports are also violations. You can appeal to the moderation team, whose discretion is final; moderation considers context, intent, severity, history and risk, and serious violations can lead to immediate suspension.
- LS.7: the platform collects the logs and metrics needed to operate, secure and improve the service, such as timestamps, device and browser information, and action metadata; it may publish aggregated transparency reports; and it may retain and disclose necessary information to comply with the law or prevent harm.
- LS.8: health, legal and financial content should avoid professional advice, and dangerous or misleading instructions may be removed; debate on civic affairs is allowed, but misinformation about participation, such as false voting arrangements, may be labelled, downranked or removed.
- LS.9: users can delete their account in settings, though some content or logs may be retained for legal or operational needs; the platform may suspend or terminate access for violations or to protect the service and its users.
- LS.10: the platform is provided as is, with no warranties; to the extent permitted by law it accepts no liability for indirect or consequential loss.

## 7. Inconsistencies in the source material and items awaiting official confirmation

The following problems genuinely exist in the pages as read, and must not be corrected on your own initiative or used to generate definite 2026 arrangements.

1. **The Prizegiving page is explicitly a placeholder.** The page states that it still uses 2025 content, reused while the 2026 competition was being written, and that nothing on it is final. The event already held on 2025-10-31, the four finalist teams, the seven-minute presentation plus five-minute Q&A per team, the three judges, the joint third place, the historical winning teams, and the accommodation and travel arrangements are all a 2025 retrospective, not 2026 requirements. Do not use them to appoint 2026 judges or set presentation lengths.
2. The Rules heading says 2026 v1.0.0, but 7.3 and 7.4 still state the Sydney prizegiving and accommodation as **2025-10-31**; this must not be silently changed to 2026-10-31, and users must not be booked onto an expired date. The 2026 date has to be confirmed by the organisers.
3. The Rules provide for the top three teams as finalists; the four teams and joint third place on the Prizegiving page are historical, and this must not be turned into a four-team 2026 final.
4. E.3 recommends the hashtag `#CaptureTheNarrative2026`, while the E.8 example still uses `#CaptureTheNarrative2025` and also mentions a September 19 start without stating a year. Old examples must not be treated as the official 2026 schedule; the promotion period follows the formal schedule in E.5.
5. The Legit Social terms state an effective date of **2025-09-10 AEST**; keep that original labelling rather than changing it to 2026.
6. The Rules page numbers both the allowed-actions list and the prohibited-actions list as **5.2**; when citing them, say which one is meant, and do not renumber them yourself as if that were the official clause numbering.
7. The Setting gives the presidential vote as **52.28% to 48.72%**, and the two sum to 101%. Record this as written and do not invent a correction; it is accurate to call the election very close, and if the exact percentages are used, the inconsistency should be noted and confirmed with the organisers.

## 8. Putting this into practice later

The following are execution prompts for applying the above to this project, not new competition terms:

- Before starting, determine whether the task belongs to the main competition, the separate promotion award or finals preparation; confirm the assigned faction, that the target really is a competition system explicitly provided by CtN, the latest relevant announcements, and the known API limits.
- When developing automation, keep platform logging and team attribution intact, do not circumvent limits, telemetry or the sandbox; protect keys and do not leak them outside the team.
- Keep the code, scripts and tools actually used, so that the finals source-code submission requirement can be met. Do not equate saving something locally with publishing it on Git or being authorised to upload it.
- In strategy analysis, distinguish simulated-citizen interactions, narrative influence and the tie-breaking criteria, and do not optimise against a scoring formula that does not exist.
- In scenario material, distinguish official facts from faction propaganda. Fictional or misleading content that is allowed inside the competition must not be turned around and used as real competition background, real medical advice or real-world promotional fact.
- When you hit an infrastructure vulnerability, a compliance boundary or a contradiction between pages, report or ask through the official channels rather than exploiting it first or deciding unilaterally. Actual contact or submission of materials still follows the user's authorisation for that action.
