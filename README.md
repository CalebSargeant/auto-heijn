# auto-heijn

Turns an Apple Reminders shopping list into Albert Heijn's Mijn lijst. A background job on your Mac keeps
one reminder in that list, "Add N items to AH", whose link adds the open items on ah.nl. You check out
yourself. It never logs in to AH, stores no secret, and cannot see or place orders.

Not affiliated with Albert Heijn. Product search uses the anonymous API behind the AH app, which AH does
not document and may change.

## Install

```sh
brew install magmamoose/tap/auto-heijn
brew services start auto-heijn
```

It runs every 5 minutes while you are logged in. The first run makes macOS ask whether `python3` may use
Reminders: click Allow. That grant covers any `/usr/bin/python3` that launchd starts for your user, not
just this tool.

Reminders sync through iCloud, so run it on a Mac signed in to the Apple Account that owns the list.

## Use

1. Add items to the list as usual. `2x melk`, `melk x2` and `melk 2x` mean two.
2. Open "Add N items to AH" and tap its link while logged in to ah.nl, **with no delivery slot booked**.
   With a slot booked, AH adds the items to that order, which changes what you pay.
3. Tick the reminder. That marks the items in the link done. Items it couldn't match stay open.

The link opens in the browser on purpose (note the `//` in it): the AH app takes the normal link and adds
nothing.

## Configure

`~/.config/auto-heijn/config.json`, all optional:

```json
{
  "list": "Shopping List",
  "products": {"melk": 1525, "shampoo": null}
}
```

`list` is the Reminders list to read. `products` maps an item name to an AH product id, matched
case-insensitively without the quantity. `null` means "not from AH": the item stays out of the link.

Items not in `products` get AH's first search hit, listed under "Guessed" in the reminder's notes.
Guesses are cached in `~/Library/Application Support/auto-heijn/state.json`. To find an id:

```sh
auto-heijn --search "halfvolle melk"
```

## Run by hand

```sh
auto-heijn --dry-run                          # show what would change, write nothing
tail -f "$(brew --prefix)/var/log/auto-heijn.log"
/usr/bin/python3 -m unittest                  # tests, from a checkout
```

## Limits

- It can't see AH orders, so checking that no slot is booked is up to you.
- One link carries at most 25 products; more products make a second reminder.
- Ticking the reminder is the only signal that the items reached AH. Tick it only after tapping.
