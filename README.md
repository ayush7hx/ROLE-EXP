# ROLE-EXP

A focused Discord bot with two features:

1. Advanced private ticket system with embeds, a dedicated `Tickets` category, staff access, claim/close controls, and transcript logs.
2. Role audit logging: only a verified human manually adding or removing a role from another human member is logged with the target member, role, executor from Discord Audit Log, IDs, and UTC timestamp. Bot actions, autorole, onboarding, and unknown/system audit events are ignored.

## Discord setup

Create a bot in the Discord Developer Portal and enable **Server Members Intent**, **Message Content Intent**, and **View Audit Log** permission. Invite it with `Manage Channels`, `Manage Roles`, `View Audit Log`, `Send Messages`, `Embed Links`, `Attach Files`, and `Read Message History`.

The bot's highest role must be above any staff role it needs to use. Discord role logs can show `Unknown / Discord system` when the bot lacks `View Audit Log` or Discord has not yet exposed the matching audit entry.

## Local run

```powershell
Copy-Item .env.example .env
# Put your token in .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

## Commands

- `*ticketconfig #ticket-logs @Staff` creates/configures the ticket category and ticket log channel.
- `*rolelog #role-logs` configures only the manual role add/remove log channel.
- `*rolelogoff` turns off manual role add/remove logs.
- `*ticketconfig #ticket-logs @Staff` configures the ticket system.
- `*ticketpanel` posts the ticket embed panel in the current channel.
- `*ticketclose` closes the current ticket (owner fallback).
- `*ticketclaim` claims the current ticket; the ticket button is also available.

Only owner ID `1255716509443948648` can use bot commands. Ticket members can use the **Close** button. Staff can use **Claim** and **Close**. A transcript file is sent to the configured ticket log channel before the ticket channel is deleted.

## Render

This repository is configured as a Render Web Service. Create a service from the repository, use the included `render.yaml`, and add `DISCORD_TOKEN` as a secret environment variable. Render's health check URL can be `/health`; the process keeps an HTTP server alive on `PORT` while the Discord client runs.
