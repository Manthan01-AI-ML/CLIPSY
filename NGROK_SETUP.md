# ClipWise — Share with Your Friend via ngrok

Complete walkthrough to expose your local ClipWise to the internet so
your friend (or anyone) can test it from their phone/laptop.

─────────────────────────────────────────────────────────────────
STEP 1 — Install ngrok (one-time, ~3 minutes)
─────────────────────────────────────────────────────────────────

You're on Windows. Easiest path:

  1. Go to https://ngrok.com/download
  2. Click "Windows"
  3. Download the ZIP
  4. Unzip anywhere (e.g., C:\ngrok\)
  5. That folder now contains `ngrok.exe`

ALTERNATIVE (if you have Chocolatey):
   choco install ngrok

─────────────────────────────────────────────────────────────────
STEP 2 — Create free ngrok account + get auth token
─────────────────────────────────────────────────────────────────

  1. Go to https://dashboard.ngrok.com/signup
  2. Sign up (free tier is totally fine for now)
  3. After login, go to: https://dashboard.ngrok.com/get-started/your-authtoken
  4. Copy the token (looks like:  2a1B2c3D4e5F6g7H8i9J_abCdEf12345678)

Now tell ngrok who you are. In PowerShell:

   cd C:\ngrok
   .\ngrok config add-authtoken 2a1B2c3D4e5F6g7H8i9J_abCdEf12345678

Expected output:
   Authtoken saved to configuration file: C:\Users\<you>\AppData\Local\ngrok\ngrok.yml

─────────────────────────────────────────────────────────────────
STEP 3 — Start ClipWise (if not already running)
─────────────────────────────────────────────────────────────────

   cd "C:\Resume Projects\Clipwise\clipwise"
   docker-compose up

Wait until all 4 containers are healthy. You should be able to
open http://localhost:8000 in your browser and see ClipWise.

Leave that terminal running.

─────────────────────────────────────────────────────────────────
STEP 4 — Open ngrok tunnel (in a NEW terminal)
─────────────────────────────────────────────────────────────────

Open a SECOND PowerShell window. Then:

   cd C:\ngrok
   .\ngrok http 8000

You'll see something like:

   Session Status              online
   Account                     your-email@example.com (Plan: Free)
   Version                     3.x.x
   Region                      India (in)
   Latency                     12ms
   Web Interface               http://127.0.0.1:4040
   Forwarding                  https://abcd-1234-567-89.ngrok-free.app -> http://localhost:8000

⚠ COPY THE URL on the "Forwarding" line — something like:
     https://abcd-1234-567-89.ngrok-free.app

That's your PUBLIC link. Send it to your friend. They can open it
from any device, anywhere in the world.

─────────────────────────────────────────────────────────────────
STEP 5 — Configure CORS so the tunnel actually works
─────────────────────────────────────────────────────────────────

The first time your friend opens the link, they'll see an ngrok
interstitial page ("You are about to visit..."). They just click
"Visit Site". This is a free-tier thing, nothing to do with your app.

After that, the site loads but API calls may fail with CORS errors.
Fix: add the ngrok URL to your `.env`:

   1. Open C:\Resume Projects\Clipwise\clipwise\.env
   2. Add a new line (or edit if it exists):

        EXTRA_CORS_ORIGINS=https://abcd-1234-567-89.ngrok-free.app

      Multiple origins? Separate with commas, no spaces:

        EXTRA_CORS_ORIGINS=https://abcd-1234-567-89.ngrok-free.app,https://another-tunnel.ngrok-free.app

   3. Save, then restart:

        docker-compose down
        docker-compose up

   Every time ngrok gives you a new URL (free tier = new URL each
   session), update this line and restart.

─────────────────────────────────────────────────────────────────
STEP 6 — Tell your friend what to expect
─────────────────────────────────────────────────────────────────

Text your friend something like:

  "Bro, test my app: https://abcd-1234-567-89.ngrok-free.app
   Click 'Visit Site' on the warning page. Sign up with any email
   (it's local only, you'll be fine). You get 3 free credits.
   Paste any YouTube link and click Generate — takes ~3-6 min."

─────────────────────────────────────────────────────────────────
COMMON ISSUES + FIXES
─────────────────────────────────────────────────────────────────

❌ "ngrok: command not found"
   → You didn't cd into the ngrok folder. Do it.

❌ URL changes every time I restart ngrok
   → That's free tier. Paid ngrok ($8/mo) gives static URLs.
     For now, send fresh link each session.

❌ "Too many connections" or slow loading
   → Free tier limit: 40 connections/minute.
     Upgrade or limit testers to 1-2 people.

❌ Friend sees "CORS error" in browser console
   → You forgot Step 5. Add ngrok URL to BACKEND_CORS_ORIGINS and restart.

❌ Video upload fails from friend's phone
   → Free tier also caps request size at ~25 MB inbound.
     Ask friend to stick to YouTube URLs (no upload) for now.

❌ "ERR_NGROK_6024" — abuse detection
   → Your account might be on a watchlist. Try a paid plan or
     new email.

─────────────────────────────────────────────────────────────────
SHUTTING DOWN
─────────────────────────────────────────────────────────────────

   In the ngrok terminal, press Ctrl+C
   In the docker-compose terminal, press Ctrl+C, then:
     docker-compose down

Your tunnel is now closed. URL is dead.

─────────────────────────────────────────────────────────────────
PRO TIP — Request inspector
─────────────────────────────────────────────────────────────────

While ngrok is running, open:
   http://127.0.0.1:4040

You'll see EVERY request your friend makes, in real time. Super
useful for debugging "it doesn't work on my phone" type complaints.

─────────────────────────────────────────────────────────────────

That's it. Friend should be testing within 10 minutes.
Once you have feedback, hit Claude with what they said.

─ Session A ─
