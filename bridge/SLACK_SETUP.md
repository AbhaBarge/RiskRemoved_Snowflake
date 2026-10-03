# Slack Bot Setup Guide -- Risk Copilot "Ask the Data"

Connect the Risk Copilot to Slack so analysts can ask data questions from any Slack channel.

---

## Prerequisites

- Python 3.9+
- A Slack workspace (free tier is fine)
- (Optional) Snowflake credentials for live mode. Without them, the bot runs in demo mode with realistic canned responses.

---

## Step 1: Create the Slack App

1. Go to **https://api.slack.com/apps**
2. Click **Create New App** > **From a manifest**
3. Select your workspace
4. Paste this JSON manifest:

```json
{
  "display_information": {
    "name": "RiskCopilot",
    "description": "AML Risk & Compliance Copilot",
    "background_color": "#1e40af"
  },
  "features": {
    "bot_user": {
      "display_name": "RiskCopilot",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "app_mentions:read",
        "chat:write",
        "channels:history",
        "channels:read"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": ["app_mention"]
    },
    "org_deploy_enabled": false,
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

5. Click **Next** > **Create**

---

## Step 2: Collect the 3 Tokens

You need 3 values from the Slack app settings. Here's where to find each one:

### Token 1: Signing Secret

- Go to **Basic Information** (left sidebar)
- Under **App Credentials**, find **Signing Secret**
- Click **Show** and copy it

```
Where to paste:  .env file → SLACK_SIGNING_SECRET=<paste here>
Looks like:      d3a94d62f4a7160b7507ae73332f2ecb
```

### Token 2: App-Level Token (for Socket Mode)

- Stay on **Basic Information**
- Scroll down to **App-Level Tokens**
- Click **Generate Token and Scopes**
- Token Name: `socket-token`
- Click **Add Scope** > select `connections:write`
- Click **Generate**
- Copy the token

```
Where to paste:  .env file → SLACK_APP_TOKEN=<paste here>
Starts with:     xapp-1-...
```

### Token 3: Bot User OAuth Token

- Go to **OAuth & Permissions** (left sidebar)
- If you see **Install to Workspace**, click it and approve
- Copy the **Bot User OAuth Token**

```
Where to paste:  .env file → SLACK_BOT_TOKEN=<paste here>
Starts with:     xoxb-...
```

---

## Step 3: Create the .env File

```bash
cd RiskRemoved_Snowflake/bridge
cp .env.example .env
```

Open `.env` and fill in your 3 Slack tokens:

```ini
# --- REQUIRED: Slack tokens ---
SLACK_BOT_TOKEN=xoxb-YOUR-BOT-TOKEN-HERE
SLACK_APP_TOKEN=xapp-YOUR-APP-TOKEN-HERE
SLACK_SIGNING_SECRET=your-signing-secret-here

# --- OPTIONAL: Snowflake (for live mode) ---
# SNOWFLAKE_ACCOUNT=hqrwrxk-uz26728
# SNOWFLAKE_USER=RAGHAV2310
# SNOWFLAKE_ROLE=RC_SERVICE_ROLE
# SNOWFLAKE_WAREHOUSE=RC_DEMO_WH
# SNOWFLAKE_PRIVATE_KEY_PATH=C:\path\to\rsa_key.p8

# --- Mode ---
DEMO_MODE=true    # Set to "false" when Snowflake credentials are ready
```

> **Security:** The `.env` file is gitignored. Never commit tokens to the repository.

---

## Step 4: Install Dependencies and Start

```bash
cd RiskRemoved_Snowflake/bridge
pip install -r requirements.txt
python slack_bot.py
```

You should see:

```
2026-10-03 10:00:00 INFO Starting Risk Copilot Slack bot (DEMO mode)...
2026-10-03 10:00:00 INFO Semantic view: RISK_COPILOT.CURATED.SV_RISK_COPILOT
```

The bot is now connected to Slack via Socket Mode. No ngrok or public URL needed -- it connects outbound from your machine.

---

## Step 5: Invite the Bot to a Channel

1. In Slack, create a channel called `#compliance-investigation` (or use any existing channel)
2. In the channel, type: `/invite @RiskCopilot`
3. The bot is now listening in that channel

---

## Step 6: Test It

Type in the channel:

```
@RiskCopilot Which branch has the most cash deposits?
```

The bot will respond with:
- The answer text
- A data table
- The SQL that Cortex Analyst generated
- A footer showing latency and mode (demo/live)

### Example Questions to Try

| Question | What It Demonstrates |
|----------|---------------------|
| `@RiskCopilot Which branch has the most cash deposits?` | Branch-level aggregation |
| `@RiskCopilot How many cases are there by status?` | Case pipeline status |
| `@RiskCopilot Show the top 5 customers by cash deposits` | Customer-level ranking |
| `@RiskCopilot How many signals have fired?` | Signal detection stats |
| `@RiskCopilot What is the status of the latest STR?` | STR generation status |

---

## Switching to Live Mode

When Snowflake credentials are available:

1. Edit `.env`:
   ```ini
   DEMO_MODE=false
   SNOWFLAKE_ACCOUNT=hqrwrxk-uz26728
   SNOWFLAKE_USER=RAGHAV2310
   SNOWFLAKE_PRIVATE_KEY_PATH=C:\path\to\rsa_key.p8
   SNOWFLAKE_ROLE=RC_SERVICE_ROLE
   SNOWFLAKE_WAREHOUSE=RC_DEMO_WH
   ```

2. Restart the bot:
   ```bash
   python slack_bot.py
   ```

3. You should see `Starting Risk Copilot Slack bot (LIVE mode)...`

The bot now calls the real Cortex Analyst API against `SV_RISK_COPILOT`, runs the generated SQL, and returns live data.

---

## Architecture

```
Slack (Priya types in #compliance-investigation)
  │
  │  Socket Mode (outbound connection, no public URL)
  ▼
slack_bot.py (on your machine)
  │
  │  Calls analyst_client.ask(question)
  ▼
analyst_client.py
  │
  ├── DEMO_MODE=true  → returns canned responses (no Snowflake needed)
  │
  └── DEMO_MODE=false → POST /api/v2/cortex/analyst/message
                         │
                         ▼
                      Snowflake Cortex Analyst
                         │
                         ▼
                      SV_RISK_COPILOT (semantic view)
                         │
                         ▼
                      SQL generated → executed → results returned
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `SLACK_BOT_TOKEN not set` | Check `.env` file exists and has the xoxb- token |
| `SLACK_APP_TOKEN not set` | Check `.env` has the xapp- token (App-Level Token, not Bot Token) |
| Bot doesn't respond to @mention | Make sure the bot is invited to the channel (`/invite @RiskCopilot`) |
| `not_in_channel` error | The bot needs to be in the channel. Use `/invite @RiskCopilot` |
| Bot responds to everything | It only responds to @mentions in channels, and all messages in DMs |
| `invalid_auth` error | Regenerate tokens in Slack app settings and update `.env` |
| Snowflake connection fails | Check account, user, and key path in `.env`. Try `DEMO_MODE=true` first. |

---

## File Structure

```
bridge/
  .env.example       # Template -- copy to .env and fill in tokens
  .env               # Your actual tokens (gitignored, never committed)
  .gitignore         # Ensures .env is not committed
  requirements.txt   # Python dependencies
  slack_bot.py       # The Slack bot (entry point)
  analyst_client.py  # Cortex Analyst client (live + demo mode)
```
