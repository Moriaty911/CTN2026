# English Discord review interface

The Bot maintains one **CTN · Report review** panel in the configured team channel. Routine actions use buttons, selection menus and short forms. The panel returns after a restart; `/reports` is an optional way to reopen a private copy.

| Main button | What it opens | What to do next |
|---|---|---|
| Review queue | Paginated candidates, initially awaiting review | Choose a candidate from the menu |
| Pending reports | Submissions in progress or awaiting an official result | Choose a submission to inspect it |
| Reporting controls | Team availability, suspension and pause status | Administrators can confirm availability or pause new reports |
| Refresh | Current summary | Read the updated status |
| Help | The complete review sequence and permissions | Return to the main panel |

Private lists and forms explain their purpose. The main shared panel updates when state changes. Candidate notifications also have their own review buttons; they do not attach JSON by default.

## One-message navigation

Opening the shared launcher creates one private workspace for you. Queue selection, evidence pages, filters, back buttons, form results, confirmations and error notices update that same message. Returning to the launcher reuses your active private workspace; it does not overwrite a teammate's view or the shared launcher.

An explicit slash-command reopen replaces the tracked previous private workspace. An evidence download is attached to the current panel and cleared on the next navigation. The private workspace closes after ten minutes idle; decisions and audit history remain saved. Expired/unavailable Discord webhooks require a replacement panel. Private interaction tokens are held in memory, so a Bot restart may require reopening; older private messages from the previous version can be dismissed manually.

## Candidate review

1. Choose a candidate from **Review queue**. The card shows the source content, affiliation provenance, evidence count, current blocking conditions and the next step.
2. Open **Evidence**. Use **Previous evidence** and **Next evidence** to read every observed/supporting post. **Download text** explicitly exports readable evidence and recorded signal details.
3. Choose **Approve for 5 min**, **Reject**, or **Defer 30 min**. Rejection and deferral open a reason form. The result states what changed and what happens next.

Approval is specific to the current evidence version. It does not itself submit a report or confirm official availability. Reviewed cards disable decision buttons but keep evidence and navigation available. A stale click explains that the card must be refreshed; it cannot create a second approval.

Queue filters include awaiting review, approved, deferred, rejected and all history. Counts refer to the full selected set; **Previous** and **Next** navigate without copying IDs.

## Team controls

**Confirm availability** opens a form for the actual official source and observation time. This creates a five-minute manual confirmation and clears a manual pause. It does not independently verify the source, enable disabled reporting, or override a six-hour suspension. Individual approval and final sending checks remain required.

**Pause new reports** stops new submissions and clears the current availability confirmation. Existing pending/uncertain submissions keep their slots. The result explains how to resume when appropriate.

## Official results

Open **Pending reports**, select a submission, then choose the official outcome. For posts, choose **Report upheld** or **Post ruled genuine**. For account reports, choose **Report upheld** or **Account report dismissed**.

Enter the official source and decision time, including its time zone. Inspect the English preview, then **Confirm official ruling** or **Cancel preview**. Only the administrator who prepared a preview can confirm or cancel it. A post ruled genuine starts the six-hour team reporting suspension. Duplicate official records remain idempotent.

An uncertain submission must be reconciled rather than resent. After 24 hours, an unresolved submission still occupies its slot.

## Permissions and compatibility

Channel members can view panels. Configured reviewers/admins can decide candidates. Only configured admins can change team controls or record official outcomes. Every action rechecks the current actor, guild and channel, including form submission and final confirmation.

The Bot requests guild metadata for role resolution; message-content and member-list privileged intents remain disabled. Buttons on shared candidate cards survive restarts. Temporary private views expire after ten minutes and can be reopened from the main panel.

`/reports`, `/report`, `/reporting` and `/ruling` remain available. All can open guided views without required arguments. Existing complete argument-based calls remain compatible; Bot results use English cards and explanations.

The UI does not change the reporting mode or start the competition execution services. The running setup remains reporting disabled unless the operator explicitly changes it.
