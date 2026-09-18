#!/usr/bin/env bash
# /home/kiosk/kiosk-launch.sh   (chmod +x)
#
# Disables all screen blanking, then launches Chromium locked in kiosk mode.
# Runs on desktop login; the backend and tunnel are already up via systemd.

# --- kill screen blanking / screensaver / DPMS power saving ---
xset s off          # no screensaver
xset s noblank      # never blank the framebuffer
xset -dpms          # no monitor power management

# --- wait for the backend to answer before opening the browser, so the
#     student never sees a connection-refused page on a cold boot ---
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8000/healthz >/dev/null && break
  sleep 1
done

# --- clear Chromium's "didn't shut down cleanly" nag, which would otherwise
#     appear on the kiosk screen after every power cut ---
PROFILE="$HOME/.config/chromium/Default/Preferences"
if [ -f "$PROFILE" ]; then
  sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' "$PROFILE"
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/' "$PROFILE"
fi

exec chromium-browser \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  "http://localhost:8000/kiosk"
