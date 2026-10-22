# The dashboard on your phone (Tailscale)

The dashboard listens on `127.0.0.1` only, so no one else on your Wi-Fi or the internet can open it. To use it from your phone, put both devices on your own private [Tailscale](https://tailscale.com/) network and let Tailscale pass requests from your phone to the dashboard. It's free for personal use and takes about ten minutes.

Your jobs never leave your devices: Tailscale connects the phone straight to your computer over an encrypted link, and only devices signed in to your Tailscale account can reach it.

## 1. Install Tailscale on both devices

- **Computer (Linux):** `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up` and sign in. On macOS or Windows, use the app from [tailscale.com/download](https://tailscale.com/download).
- **Phone:** install the Tailscale app (Android or iOS) and sign in with the **same account**.

## 2. Turn on HTTPS for your network

In the [Tailscale admin console](https://login.tailscale.com/admin/dns): **DNS** → make sure **MagicDNS** is on → under **HTTPS Certificates**, click **Enable HTTPS**. This gives your computer a name like `laptop.tail1234.ts.net` with a real certificate.

## 3. Share the dashboard with your Tailscale network

With `jobradar run` running (the log says `Dashboard: http://127.0.0.1:8080`), run once on the computer:

```bash
sudo tailscale serve --bg 8080
```

It prints the address, for example `https://laptop.tail1234.ts.net/`. `--bg` keeps it on after reboots. If the log said JobRadar took another port (8081 …), use that number, or set `web.port` so it doesn't move.

Use `serve`, **never `funnel`**: `funnel` puts the page on the public internet.

## 4. Tell JobRadar the address

In `config/config.yaml`:

```yaml
web:
  url: https://laptop.tail1234.ts.net   # the address `tailscale serve` printed
```

Restart `jobradar run`. Now:

- the dashboard accepts requests for that name (it refuses every other name, which is what stops other websites from reaching it);
- **View in dashboard** links in alert and digest emails use it, so they open on your phone too.

## 5. Open it on the phone

With Tailscale connected on the phone, open the address in the browser. Add it to the home screen (Chrome: **⋮ → Add to Home screen**; Safari: **Share → Add to Home Screen**) to open it like an app. On the phone you get the bottom tab bar, a sideways Applications board, and swipe right to shortlist or left for Not interested on Today.

## Turning it off

```bash
sudo tailscale serve --https=443 off
```

Then remove `web.url` from `config.yaml` and restart `jobradar run`. `tailscale serve status` shows what's being shared.

## If it doesn't open

| What you see | Why, and what to do |
|---|---|
| The page doesn't load | Tailscale isn't connected on the phone, or `jobradar run` isn't running. Check the Tailscale app and `tailscale serve status`. |
| "Invalid host header" | `web.url` isn't set, or doesn't match the address exactly. Fix it and restart `jobradar run`. |
| Buttons say the request was refused | Same cause: the page's address must match `web.url`. |
| "502 Bad Gateway" | Tailscale is sharing a port JobRadar isn't on. Compare the port in JobRadar's log with `tailscale serve status`. |
