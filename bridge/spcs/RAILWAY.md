# Host the Slack bot on Railway

The bot runs in Socket Mode: it dials out to Slack, so Railway needs no public
domain or port. One Railway service = one always-on bot.

The image is `bridge/spcs/Dockerfile` (selected by `railway.json` at the repo root).
`.env` and `keys/` are gitignored, so they never reach GitHub or the Railway build;
secrets come in as Railway variables instead.

## 1. Make the variables file (on your laptop)

```powershell
.\bridge\.venv\Scripts\python.exe bridge\spcs\railway_env.py
```

This writes `bridge/railway.env` (gitignored): the 22 variables from `bridge/.env`,
with both `.p8` keys inlined as `SNOWFLAKE_PRIVATE_KEY_PEM` / `AGENT_PRIVATE_KEY_PEM`.
It contains secrets: don't commit or share it, and delete it when Railway has the values.

## 2. Create the Railway service

1. Push the branch you want to deploy to GitHub.
2. railway.com > **New Project** > **Deploy from GitHub repo** > `RiskRemoved_Snowflake`.
3. Service **Settings** > **Source**: pick that branch. Leave the root directory empty
   (the repo root), since the build needs `src/` and `conf/` too.
4. Service **Variables** > **Raw Editor**: paste the whole of `bridge/railway.env` > **Update Variables**.
5. No domain is needed: skip **Networking**.

Railway builds the image and starts the bot. **Deployments** > **View logs** should show
`Bolt app is running!` and `Starting to receive messages from a new connection`.

## 3. Switch over

Stop any local copy (`Ctrl+C` in its terminal). One Slack app = one running bot: with
two connected, Slack hands each message to only one of them.

## Updating

- Code change: push to the branch; Railway rebuilds and redeploys.
- Token or key change: rerun step 1 and paste again (or edit that one variable).

## Troubleshooting

| Log line | Fix |
| --- | --- |
| `SNOWFLAKE_PRIVATE_KEY_PEM is empty` | The key variables weren't pasted; repeat step 2.4. |
| `invalid_auth` | Slack tokens wrong or from different Slack apps; regenerate `railway.env` from a correct `bridge/.env`. |
| `JWT token is invalid` | The public key on the Snowflake user doesn't match the pasted private key. |
| Connected, but messages never logged | Slack app settings (events `app_mention` / `message.im`, reinstall): see `bridge/SLACK_SETUP.md`. |

`bridge/spcs/deploy.py` is the Snowpark Container Services route for the same image.
It needs a non-trial Snowflake account: trials reject external access integrations.
