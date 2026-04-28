PICKUP PROMPT - Pirate Dock Human-In-The-Loop Browser
=====================================================

Current architecture
--------------------
- Repo: `~/Documents/GitHub/pirate-dock`
- Container: `pirate-dock`
- VPN: NordVPN runs inside the container only. The host Pi must not join the VPN.
- Browser: Playwright/Chromium runs inside the container so browser traffic uses the VPN.
- Display: `run.sh` starts `Xvfb :1` and `xpra shadow :1` inside the container.
- Xpra HTML5: container port `6081`, published by Docker as host port `6081`.
- Human URL: `https://araminta.taild3f7b9.ts.net:8443/pirate/`

Critical invariant
------------------
Minty must be able to send David a WhatsApp with a URL that opens the live
container browser. Without that, Pirate Dock is not useful for CAPTCHA, login,
or visual confirmation flows.

Expected Tailscale Serve config:

```bash
sudo tailscale serve status
# https://araminta.taild3f7b9.ts.net:8443 (tailnet only)
# |-- /pirate proxy http://127.0.0.1:6081
```

Repair command:

```bash
sudo tailscale serve --bg --https=8443 --set-path=/pirate 6081
```

Principles
----------
1. All browser/download/search traffic originates from inside `pirate-dock`.
2. Do not install or run NordVPN on the host Pi.
3. Do not move the browser to the host to make display sharing easier.
4. Keep Tailscale Serve path `/pirate` pointed at `6081`; do not rely on stale helper ports or overwrite unrelated root routes.
5. Update `SKILL.md`, `README.md`, compose, and scripts together when changing this flow.

Verification
------------
```bash
docker ps --filter name=pirate-dock
docker exec pirate-dock curl -sS -I http://127.0.0.1:6081/index.html
sudo tailscale serve status
```

From laptop or phone on the Tailnet, open:

```text
https://araminta.taild3f7b9.ts.net:8443/pirate/
```
