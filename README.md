# auto-heijn

Fills your Albert Heijn basket from an Apple Reminders shopping list. You check out yourself.

Two parts:

- **The server** (`server.py`, a container) matches list items to AH products with AH's product search
  and an LLM, adds them to your AH basket through the API behind the AH app, and posts what it added to
  Slack.
- **The bridge** (`auto_heijn.py`, a Homebrew service on a Mac) sends the open reminders to the server
  every few minutes and ticks the ones the server put in the basket. Reminders only exist on Apple
  devices, so this part has to run on a Mac.

Not affiliated with Albert Heijn. AH doesn't document its app API and may change it.

## What it won't do

- Check out or place an order. It only adds items to the basket.
- Add anything while an order is open or a delivery is booked. With a slot booked, AH puts new items in
  that order, which changes what you pay. The items stay on the list until the delivery is done.
- See your AH password. You log in yourself in a browser and paste back the login code; the server keeps
  AH's refresh token.

## Server

```sh
docker run -p 8080:8080 -e BRIDGE_TOKEN=... -e DATABASE_URL=... ghcr.io/calebsargeant/auto-heijn:<version>
```

| Variable | |
|---|---|
| `BRIDGE_TOKEN` | Shared secret the bridge sends. Also the password for the web page (any user name). |
| `DATABASE_URL` | `postgresql://...`, or a SQLite file path for local runs (default `auto-heijn.db`). |
| `AH_WRITE` | `true` to add to the basket. Default `false`: it reads the basket and reports what it would add. |
| `PUBLIC_URL` | The server's address, used in Slack messages. |
| `LLM_URL`, `LLM_MODEL`, `LLM_API_KEY` | An OpenAI-compatible chat endpoint (`/v1/chat/completions`). |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` | Where it posts. The bot needs `chat:write` and must be in the channel. |
| `PORT` | Default `8080`. |

Run it where AH sees a home internet connection. AH's bot protection is stricter with datacenter
addresses. Keep it off the public internet: the web page and API only check `BRIDGE_TOKEN`.

### Log in to AH

Open the server's page in a private window on a computer and follow the steps there. In short: with the
browser's developer tools on the Network tab (Preserve log on), log in on AH's own login page, then copy
the `appie://login-exit?code=...` address from the first `ingelogd.json` response and paste it into the
page within a minute. When AH stops accepting the saved login, Slack asks you to do this again.

## Bridge

```sh
brew install magmamoose/tap/auto-heijn
security add-generic-password -U -s auto-heijn -a bridge-token -w   # paste BRIDGE_TOKEN
```

`~/.config/auto-heijn/config.json`:

```json
{"list": "Shopping List", "server": "https://ah.example.com"}
```

```sh
brew services start auto-heijn
```

The first run makes macOS ask whether `remindctl` may use Reminders: click Allow. The grant belongs to
`remindctl`, not to Python. Reminders sync through iCloud, so run it on a Mac signed in to the Apple
Account that owns the list.

## Use

1. Add items to the list as usual. `2x melk`, `melk x2` and `melk 2x` mean two.
2. Within a few minutes the item is in the AH basket, Slack says so, and the reminder is ticked.
3. Items the server isn't sure about stay open, and Slack lists them with its best guess. Fix one with

   ```sh
   auto-heijn --search "pindakaas"          # find the product id
   auto-heijn --map "pindakaas" 220739      # always use that product
   auto-heijn --map "shampoo" none          # not from AH: leave it alone
   ```

4. Check out on ah.nl or in the AH app.

## Run by hand

```sh
auto-heijn --dry-run                          # what the server would add, changes nothing
tail -f "$(brew --prefix)/var/log/auto-heijn.log"
python3 -m unittest                           # tests, from a checkout
```

## Limits

- If AH takes the items but its answer times out, the next run adds them again.
- How long AH keeps a login alive is unknown.
