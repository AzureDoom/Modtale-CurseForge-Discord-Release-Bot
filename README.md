# Modtale & CurseForge Discord Release Bot

<img alt="GitHub License" src="https://img.shields.io/github/license/AzureDoom/Modtale-CurseForge-Discord-Release-Bot"><img alt="GitHub Issues or Pull Requests" src="https://img.shields.io/github/issues/AzureDoom/Modtale-CurseForge-Discord-Release-Bot"><img alt="GitHub top language" src="https://img.shields.io/github/languages/top/AzureDoom/Modtale-CurseForge-Discord-Release-Bot">


A Discord bot that automatically monitors **Modtale** and **CurseForge** projects and posts update notifications when new versions/files are released.

<img width="550" height="448" alt="image" src="https://github.com/user-attachments/assets/212d523d-bbd0-4ba6-a997-38e82c7d6760" />

It supports:
- Multiple Modtale projects
- Multiple CurseForge projects
- Either platform independently (Modtale-only or CurseForge-only)
- Persistent cache to avoid reposting old releases
- Discord embeds with download buttons

---

## What the Bot Does

- Periodically polls:
  - Modtale project APIs
  - CurseForge (via CFWidget)
- Detects **new versions/files**
- Posts a rich embed message to a configured Discord channel
- Stores seen releases in `cache.json` so each release is only posted once

---

## Requirements

- Python **3.10+**
- A Discord bot token
- API access to the Modtale project(s) you want to track. Get your Modtale api token from: https://modtale.net/dashboard/developer
- Project IDs / slugs for CurseForge projects

---

## Setup Instructions

### Clone the repository

```bash
git clone https://github.com/AzureDoom/Modtale-CurseForge-Discord-Release-Bot.git
cd Modtale-CurseForge-Discord-Release-Bot
```

### Install dependencies

```bash
pip install -r requirements.txt
```

Required libraries include:

- discord.py
- aiohttp
- python-dotenv

### Configure environment variables

You will find a file named:

```bash
example.env
```

Rename it to `.env`.

Then open `.env` and fill in your values.

You can enable:

- Only Modtale (Leave CurseForge Blank: CURSEFORGE_PROJECTS_JSON=)
- Only CurseForge (Leave Modtale Blank: MODTALE_PROJECTS_JSON=)

Leaving a project JSON variable blank disables that source.

### Run the bot

Simply do: 
```
python3 bot.py
```

On startup you should see:
```
Logged in as YourBotName
Successfully finished startup
```

## Docker Installation

Running the bot in Docker is the recommended way to deploy it for 24/7 usage. This ensures consistent behavior across environments and clean restarts.

Prerequisites
- Docker 20.10+

- Docker Compose (v2, docker compose)

Verify installation:
```bash
docker --version
docker compose version
```

### Prepare environment variables
An example environment file is included as example.env.

Rename it to `.env`

Edit .env and configure:
- Discord bot token

- Channel ID

- Modtale and/or CurseForge projects

### Build and start the container
From the project root:
```bash
docker compose up -d --build
```

The bot will:

- Build the image

- Start automatically

- Restart on failure or system reboot

### View logs
Simply run:
```bash
docker compose logs -f
```
You should see:
```bash
Logged in as <bot name>
Successfully finished startup
```

### Stop or restart the bot
Stop:
```bash
docker compose down
```

Restart:
```bash
docker compose restart
```

### Updating the Bot
When you pull new changes:
```bash
git pull
docker compose up -d --build
```

## Cache Behavior

- A file named cache.json is created automatically
- Tracks seen releases per project
- Safe to delete if you want the bot to repost everything again

## Troubleshooting

Bot starts but posts nothing:
- Ensure your project JSON is valid
- Make sure the project actually has released files
- Check that the Discord channel ID is correct

JSON parsing errors:
- Make sure all keys use double quotes
- Wrap JSON values in single quotes in .env
