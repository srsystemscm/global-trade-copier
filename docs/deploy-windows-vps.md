# Deploying the hub + IB Gateway on a Windows VPS

Build runbook for the box that runs **IB Gateway and the copier hub together**.
Written 2026-10-30, for the IBKR paper leg (`DUR314653`).

> **`deploy/` in this repo is the old Linux path** (`provision.sh`,
> `tradecopier-hub.service`, `nginx.conf`, `ufw-rules.sh`, July 2026). It
> predates the IBKR work and is *not* what this runbook uses. Ignore it, or
> delete it once this is proven.

## Why these two live on one box

IB Gateway's API socket has **no authentication**. Its only controls are
"listen on localhost" and a Trusted IPs allowlist. A remote hub would mean
either binding an unauthenticated plaintext order-entry socket to `0.0.0.0`,
or running a tunnel to fake locality. Co-locating removes the problem: the
adapter already defaults to `127.0.0.1`, so this is a one-line port change.

Second reason: Gateway force-restarts daily. On localhost a restart is
unambiguous -- the socket is gone, then it is back. Across a tunnel, a network
blip and a Gateway restart are indistinguishable to the adapter.

Three roles, three boxes:

| Box | Runs | OS |
|---|---|---|
| Existing MT4 VPS 1/3 | MT4 master terminals + MT4Monitor V2 | Windows |
| **This box (new)** | **IB Gateway + IBC + copier hub** | **Windows** |
| Lightsail `52.15.101.167` | ops_hub (Docker, Caddy, backups) | Ubuntu 24.04 |

ops_hub never touches Gateway -- it reads IBKR over the Flex Web Service
(plain HTTPS, see DECISIONS D39), so it never competes for IBKR's
one-session-per-username limit. Do **not** put Gateway on the Lightsail box:
it is your live reporting surface with a public cert, and adding a broker
session widens the blast radius of every `push.ps1`.

---

## Phase 0 -- before you create the box

1. **Check whether your IBKR login has 2FA (IBKR Mobile) enabled.** This
   decides whether the box is ever truly hands-off. See "The weekly tap"
   below. Paper logins use your real username with `TradingMode=paper`, so
   2FA policy follows the real account.
2. **Pick the plan.** Windows Server itself takes ~2 GB, Gateway's JVM
   ~1.5 GB, the hub ~150 MB.
   - **4 GB / 2 vCPU minimum.**
   - **8 GB** if any MT4 slave terminal will ever run here.
3. **Region:** match ops_hub (`us-east-2`) so there is one less variable. The
   two boxes do not talk, so this is only about your own RDP latency.

## Phase 1 -- the instance

1. Lightsail -> **Windows Server 2022**, plan from above.
2. Attach a **static IP**. Gateway does not care, but your WireGuard peers
   and RDP allowlist do.
3. **Firewall (Lightsail networking tab): RDP (3389) from your IP only, plus
   UDP 51820 for WireGuard. Nothing else.** In particular never open 4002,
   5555, 5557, 5560 or 8000 to the internet.
4. Take a snapshot once Windows is updated and before you install anything.

## Phase 2 -- autologon (this is what makes it survive a reboot)

IBC must run under **"Run only when user is logged on"** -- the IBC user guide
strongly advises against the alternative, because Gateway then runs in a
hidden session you cannot interact with. This is the same lesson MT4Monitor
already encodes: a Java/GUI app launched without a desktop either fails to
start or starts invisible, and the API socket never opens.

So the Windows session must come back by itself after a reboot:

1. Download **Sysinternals Autologon**.
2. Set it for the account that will own the Gateway/IBC/hub tasks.
3. Reboot and confirm the desktop returns with no keystrokes.

**Security note:** Autologon stores the password as an LSA secret in the
registry. It is recoverable by anything running as admin on that box. This is
acceptable *only* because RDP is allowlisted to your IP in Phase 1. If you
widen that allowlist, revisit this.

**RDP habit that matters:** disconnecting (the X, or `Disconnect`) leaves the
session running. **Choosing "Sign out" kills Gateway.** Build the habit now.

## Phase 3 -- WireGuard

`TC_ZMQ_BIND_HOST` defaults to `*` (all interfaces), and **ZMQ has no
authentication: anyone who can reach port 5557 can inject a trade signal.**
The firewall *is* the auth. Security-group allowlisting works but fails
silently when a VPS gets a new IP -- the copier looks healthy and receives
nothing. A WireGuard mesh keeps public ingress at zero.

1. Install WireGuard on this box, each MT4 master box, and your PC.
2. Addressing (suggestion):

   | Peer | WG address |
   |---|---|
   | This box (hub) | `10.8.0.1` |
   | MT4 VPS 1 | `10.8.0.2` |
   | MT4 VPS 3 | `10.8.0.3` |
   | Your PC | `10.8.0.10` |

3. This box is the only peer needing a stable endpoint; give it
   `ListenPort = 51820` and the static IP. Masters dial out with
   `PersistentKeepalive = 25`.
4. Verify with `ping 10.8.0.1` from a master box **before** touching the EAs.

Then the master EAs point at `10.8.0.1:5555` / `:5557`, and the hub binds to
the WireGuard address only (Phase 6).

## Phase 4 -- IB Gateway

1. Install the **stable** IB Gateway build (not latest).
2. **Log in once by hand** in paper mode, so the settings directory exists.
3. Configure -> **API -> Settings**:
   - `Enable ActiveX and Socket Clients` -- **on**
   - `Socket port` -- **4002** (paper; 4001 is live)
   - `Trusted IPs` -- **`127.0.0.1`** only
   - `Read-Only API` -- **off** (the copier places orders)
   - `Download open orders on connection` -- **on**. This one matters: without
     it, `openTrades()` shows only the calling client's orders, which is
     exactly what made a live protective stop look missing earlier in this
     project. `reqAllOpenOrdersAsync()` is still the reliable read.
   - `Master API client ID` -- leave blank
4. Configure -> **Lock and Exit**:
   - **Auto restart** (not auto-logoff). Auto-logoff requires you to log in
     again by hand every day.
   - **Restart time: 17:15 ET.** This sits inside CME's daily maintenance
     halt (gold trades 18:00-17:00 ET with a 60-minute break from 17:00), so
     the restart lands when you have no live session to lose. Confirm against
     the hours of whatever you actually trade.
5. Confirm the port is listening locally and **nowhere else**:

   ```
   netstat -ano | findstr :4002
   ```

   It must show `127.0.0.1:4002`, never `0.0.0.0:4002`.

## Phase 5 -- IBC

IBC supplies the credentials Gateway asks for and handles its dialogs.

1. Download IBC, install to `C:\IBC`.
2. Create `%USERPROFILE%\Documents\IBC\config.ini`:

   ```ini
   IbLoginId=<your IBKR username>
   IbPassword=<your password>
   TradingMode=paper

   AutoRestartTime=17:15

   ; re-initiate login if the 2FA prompt times out instead of sitting dead
   ReloginAfterSecondFactorAuthenticationTimeout=yes
   SecondFactorAuthenticationExitInterval=60
   ```

   The guide advises against passing credentials on the command line, so they
   live here. **ACL this folder to the single user** -- it is a plaintext
   password on a trading box.

3. In your copy of `StartGateway.bat`, set `TWOFA_TIMEOUT_ACTION=restart` so a
   missed 2FA prompt retries rather than parks.
4. **Task Scheduler:**
   - Trigger: **At log on** (of the autologon user)
   - Action: `C:\IBC\StartGateway.bat` with the **`/INLINE`** argument --
     without it Task Scheduler does not see IBC as running
   - **"Run only when user is logged on"** (not the "whether or not" option)
   - **"Run with highest privileges"** -- on
   - The task's user **must match** the Autologon user
5. Reboot. Gateway should come back with no input from you.

## Phase 6 -- the hub

Run the hub as a **scheduled task in the same session**, not a Windows
service. A service would survive the session dying -- but a dead session
means Gateway is dead too, so that independence buys nothing and costs you a
second place to look when something breaks.

1. Install **Python 3.11** (matching dev).
2. Clone the repo, then:

   ```
   py -3.11 -m venv .venv
   .venv\Scripts\pip install -r hub\requirements.txt
   .venv\Scripts\pip install ib_async
   ```

   `ib_async` is commented out of `requirements.txt` on purpose (the adapter
   lazy-imports it so non-IBKR deployments do not need it), so install it
   explicitly here.

3. `hub\.env`:

   ```
   TC_ZMQ_BIND_HOST=10.8.0.1
   TC_LOG_LEVEL=INFO
   ```

   Binding to the WireGuard address rather than `*` is the hardening step.
   Do this *after* WireGuard is up, or the bind fails at boot.

4. **Point the IBKR slave at Gateway.** Slaves live in `tradecopier.db`, not a
   config file -- edit via the UI or `POST /slaves`. The change is
   **`port: 7497` -> `4002`** (7497 is the TWS paper default; Gateway uses
   4002). `host` stays `127.0.0.1`.

5. Task Scheduler: **At log on**, same user, `.venv\Scripts\python.exe run.py`,
   working directory `hub\`. Use absolute paths -- a background task in this
   project has ignored a `cd` before.

6. The UI stays on WireGuard or an SSH tunnel, never public. Note
   `hub/app/main.py:75` hardcodes CORS to `localhost:5173`; fine through a
   tunnel, but it must become configurable before the UI gets a real hostname.

## Phase 7 -- heartbeat

Nothing running on this box can report its own death. Use the pattern
MT4Monitor already uses: a scheduled task pings **healthchecks.io** on every
check, and the *absence* of pings is what alerts.

**Use a new, separate check URL for this box.** A shared one would let any
single box keep the check green for all of them.

Ping only when the box is genuinely healthy -- at minimum, Gateway's port is
listening and the hub answers `/status`:

```powershell
$gw  = (Test-NetConnection 127.0.0.1 -Port 4002).TcpTestSucceeded
$hub = try { (Invoke-WebRequest http://127.0.0.1:8000/status -TimeoutSec 10).StatusCode -eq 200 } catch { $false }
if ($gw -and $hub) { Invoke-WebRequest "https://hc-ping.com/<uuid>" -TimeoutSec 10 }
```

Target **Windows PowerShell 5.1** -- that is what the Lightsail Windows
images ship, and PS7-only syntax has silently broken restart tracking in this
fleet before.

## Phase 8 -- verification

Do these in order, and do not skip 4.

1. `netstat -ano | findstr :4002` -> bound to `127.0.0.1` only.
2. Hub `/status` -> `ibkr-paper-1` connected.
3. Open one small GC trade on a master. Confirm it reaches the IBKR leg with
   a protective stop, read back via `reqAllOpenOrdersAsync()`.
4. **Reboot the box with a position open.** Confirm: desktop returns,
   Gateway returns, hub returns, and the position is still tracked (the
   `engine_positions` store plus `restore_position()` should rebuild it).
   This is the test that actually proves the deployment.
5. Let it sit through one 17:15 ET auto-restart. The hub log should show
   `slave_disconnected`, one or more failed recovery attempts while Gateway
   is still coming up, then `slave_session_recovered`. Confirm it still
   places orders afterward.
6. Snapshot the instance.

---

## The weekly tap

Auto-restart avoids the *daily* login, but it does not avoid the weekly one.
The IBC guide's rule: *authentication is only required the first time during
the week that TWS or Gateway run after 01:00 ET on Sunday.* Session
credentials expire then and Gateway must be fully shut down at some point on
Sunday.

IBC supplies username and password automatically. **If your account has IBKR
Mobile 2FA enabled, that weekly login needs one manual acknowledgement on
your phone, inside a ~3-minute window.** That is one tap per week and it
cannot be automated away through IBC.

Community reports also note the post-restart re-login is not perfectly
reliable -- rejected token re-logins and password-entry timeouts both show up
in IBC/controller issue trackers. Expect to need the Phase 7 heartbeat to
tell you, rather than assuming success.

---

## Session recovery: how the daily restart is handled

Gateway's daily restart kills the hub's IBKR session, and nothing in
`ib_async` rebuilds it. `SlaveWatchdog` now does.

**Which adapters.** `BrokerAdapter.requires_session_recovery` is `False` by
default and `True` only on `IBKRAdapter`. MT4 rebuilds its REQ socket on
timeout and Schwab is stateless HTTP, so neither gains a recovery loop and
both behave exactly as before.

**What recovery does.** It re-runs the slave's whole startup path --
**connect -> `reconcile_slave()` -> `restore()`** -- not a bare reconnect.
The order matters: reconcile marks registry rows CLOSED (and drops their
`position_store` entries) for anything the broker no longer holds, so restore
rebuilds only what is genuinely still open. A position stopped out while the
session was down would otherwise return as live state, and the trailing
monitor would place a stop for something the broker doesn't hold -- which on
a flat account opens a position rather than protecting one.

**Schedule.** 5s doubling to a 60s ceiling (`TC_SESSION_RECOVERY_*`). The
first attempt waits, because Gateway takes a minute or two to come back and
an immediate retry only burns an attempt.

**`session_recovery_max_attempts` is when you get told, not when it stops.**
After 8 failures it fires `slave_recovery_failed` once, then keeps retrying
at the 60s ceiling. Stopping at the cap would strand the hub through any
outage longer than the schedule -- the weekly Sunday 2FA login, which can sit
unacknowledged for hours.

**Events**, so alerting can tell routine from real:

| Event | Meaning |
|---|---|
| `slave_disconnected` | Fires on every scheduled restart. Routine -- do not alert on it. |
| `slave_session_recovered` | Back up, with the attempt count. |
| `slave_recovery_failed` | Once, after the cap. Needs you. |
| `signal_refused` | A signal arrived while the session was down. |

**Signals during the outage are refused, not queued.** OPEN and MODIFY are
dropped -- a trade signal is only valid near the moment it fired, and filling
an OPEN five minutes late at a different price is worse than missing it.

**A refused CLOSE is the one case that needs you.** It is dropped too (it
cannot be executed with a dead session, and replaying it later is unsafe: on
a netting account a close fired against a position someone already flattened
by hand would *open a reverse position*), so it raises a `close_refused`
notification instead. Recovery's reconcile pass cannot find this drift -- the
registry still says OPEN and the broker agrees, because nothing recorded the
master's exit. Treat that alert as "check both accounts by hand."

### Verified 2026-10-30

- 194 hub tests pass (182 before), 12 new in `tests/test_session_recovery.py`.
- Against real Gateway on 4002: one adapter instance, `disconnect()` then
  `connect()` immediately with the **same clientId**, no "client id already
  in use", and `get_account_summary()` worked straight after.
- **Not yet proven live:** a real 17:15 ET Gateway auto-restart end to end.
  That is Phase 8 step 5.

### Related fix

`AutonomousCopyEngine._monitor_loop()` now skips a pass while the session is
down. It previously called the adapter regardless, which raised
`AssertionError` ("adapter not connected") -- not an `AdapterError`, so it
escaped the loop's handlers and **ended the monitor task for the life of the
process**, taking every position's trailing stop with it, silently, and not
returning when the session did. Its per-position and roll handlers also catch
`Exception` now, so one bad position cannot cost the others their stops.
