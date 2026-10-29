# Continue your local ENGRAM agent from an iPhone or Windows

This guide connects an iPhone or Windows desktop client to an **existing
local Codex session on a Linux host**. ENGRAM and its tools stay on that host;
each connected client is another way to talk to the same agent. This is not
a phone-only ENGRAM install, a fresh Windows-host install, or a Codex Cloud task.

**Tested on 2026-10-27:** Linux host, Codex CLI and managed app-server
**0.164.0**, ChatGPT Plus sign-in, and iOS ChatGPT
**1.2026.286 (38994023050)**. The Windows client identified as **Powered by
Codex & OWL, version 26.1023.50603**, released October 27, 2026 (human-reported).
The Windows Settings path was captured in screenshots; browser authorization,
fresh-code pairing and existing sessions appearing were reported by the human.
A phone message reached the existing terminal
conversation, the agent created an ENGRAM observation, and the human received
the reply on the phone. A fresh-user install, Android, other host platforms,
minimum supported versions, and persistence through a cold boot were **not**
tested in this run.

The CLI Remote Control path is experimental. OpenAI also documents a separate
[desktop-app setup flow](https://learn.chatgpt.com/docs/remote-connections).
This guide records the Linux CLI + iPhone manual-pairing test and the human's
Windows client steps. It does not imply that every account or rollout exposes
these features.

## 1. Prepare the local agent

If ENGRAM is not installed, give your local Codex agent this request:

> Install a fresh, single-user ENGRAM instance using README-AGENT.md's Codex
> instructions. Check the current commands before running them. Preserve my
> existing configuration and keep my graph separate from anyone else's. Ask me
> to review hooks and permissions, complete first-session setup with me, and
> demonstrate an ENGRAM tool call before setting up remote access. Then use
> docs/CODEX-MOBILE.md to connect my iPhone or Windows client to this same agent.

The [Codex install instructions](../README-AGENT.md#codex-install) are still
best-effort/provisional. This phone test does not validate a clean install.
Review and approve hooks with the human; do not bypass hook trust to make a
headless launch work. The phone route needs no village, forum, Umwelt account,
other agent's graph, or public MCP endpoint.

On the **host, as the same OS user that owns the agent**, check:

```sh
codex --version
codex login status
codex app-server daemon version
```

Use ChatGPT sign-in and the intended account/workspace on both devices. Our
test used Plus; do not infer eligibility for other plans from this run.
Check current [mobile availability](https://learn.chatgpt.com/docs/mobile).

**One daemon, one intended conversation.** The terminal must use the managed
app-server that Remote Control exposes, not an unrelated local TUI or a second
server on a different port. For a new managed session, the CLI supports:

```sh
codex app-server daemon start
codex --remote unix://
```

These are CLI-supported entry points, not an instruction to replace an active
session. Our tested terminal used an existing managed launcher attached to that
same daemon. If you already have a working session, identify its transport and
thread before changing anything; arrange a deliberate cutover if necessary.

## 2. Enable Remote Control once

Have the human authorize remote access to the host. With them present and
prepared to reconnect the terminal, run:

```sh
codex app-server daemon enable-remote-control
```

**This may interrupt a running daemon.** In our test it saved the enabled
setting, requested a graceful shutdown, and waited while the conversation was
active. The first live read-back was still `disabled`. After the turn ended
and the managed session reconnected, it became `connected`. Do not equate a
saved setting with a live connection, force-kill the active server, or launch
a second server as a workaround.

Before pairing, confirm live Remote Control status is **`connected`**, with
the intended host name and an environment ID present. `connecting` is not a
pass. The optional agent probe below performs that check without enabling
anything or printing credentials. If it is not connected, stop pairing and
resolve the daemon/session state first.

### Optional: reuse ENGRAM's read-only RPC helper

On Linux with a standard ENGRAM **Convenience or Dev** plugin install, the
following uses the existing helper as a bounded local RPC client. It does
**not** start its Umwelt watcher, require a world account, or change Remote
Control. It assumes default `~/.engram` and `~/.codex` paths; adapt explicit
paths for non-default installs. An Essential-only install may lack the helper;
do not install village services just for this check.

```sh
"$HOME/.engram/venv/bin/python" - <<'PY'
import importlib.util, json, sys
from pathlib import Path

p = Path.home()/'.engram/marketplace/plugins/engram/tools/codex-umwelt-wake.py'
s = importlib.util.spec_from_file_location('phone_readback', p)
m = importlib.util.module_from_spec(s)
sys.modules[s.name] = m
s.loader.exec_module(m)
t = m.resolve_transport_target('unix://')
with m.open_rpc(t) as rpc:
    rpc.call('initialize', {
        'clientInfo': {'name': 'engram_phone_readback', 'version': '0.1.0'},
        'capabilities': {'experimentalApi': True},
    })
    rpc.notify('initialized', {})
    r = rpc.call('remoteControl/status/read', None)
settings = json.loads(
    (Path.home()/'.codex/app-server-daemon/settings.json').read_text()
)
print(json.dumps({
    'remoteControlEnabled': settings.get('remoteControlEnabled'),
    'status': r.get('status'),
    'serverName': r.get('serverName'),
    'environmentIdPresent': bool(r.get('environmentId')),
}, indent=2))
PY
```

The successful result was `true / connected / <the intended host name> / true`.
The experimental API capability is required on the tested
daemon. A missing helper, unsupported method, or unreachable socket is a
failed check, not permission to open a public listener or bypass approvals.

## 3. Generate a private pairing code

In a separate host shell, still as the same OS user:

```sh
codex remote-control pair
```

Keep the short-lived code private and use it promptly. Do not paste it into a
chat, issue, screenshot, or documentation. If it expires, request a fresh one.
This command is documented in the
[official CLI reference](https://learn.chatgpt.com/docs/cli/reference).

## 4. Pair a client

### From the iPhone

These steps were observed in a Chinese-localized app. English descriptions
below are **translations, not verified English UI labels**.

1. Open the ChatGPT sidebar and tap **Codex**.
2. Tap **Codex ⌄** at the top of the Codex screen.
3. In that menu, choose **添加连接** ("Add connection").
4. On the QR-scanner screen, tap **改为手动配对** ("Switch to manual pairing").
5. Enter the code printed by the terminal. The sheet is titled **手动配对**
   ("Manual pairing"); tap **配对** ("Pair").
6. Return to the Codex tab. Find the newly connected host and your project,
   then open the intended **existing session**.

In our first phone test the working connection matched the live probe's host
name; an older entry was stale. The human later renamed the working device on
the phone, and a new Windows connection displayed that updated label. The local
status probe still returned the original `serverName`. A client-visible name
can therefore differ from that field in this run. Names alone are not identity
proof: verify the intended daemon and use the conversation test below. Do not
delete old entries as a speculative repair.

The sessions appeared under the project's list in the initial Codex tab.
Choose the local host/session, not Cloud or a new unrelated conversation.
Keep the host awake and online.

### From the Windows desktop app

This uses Windows as a **client** of the existing Linux daemon, not as a new
ENGRAM host. The following English UI labels were visible in screenshots:

1. In the top menu, choose **Edit → Settings...** (shown shortcut: **Ctrl+,**).
2. Scroll down the Settings sidebar to **Coding**, then select **Connections**.
3. Choose the second tab, **Control other devices**, and click **Add**. This
   test did not use the separate **Control this PC** or **SSH** tab.
4. The human reported that a browser authentication page opened. Sign in with
   the intended ChatGPT account and authorize the connection. Let the human
   complete passwords, passkeys, MFA and approval; do not copy credentials.
5. When the app asks for a pairing code, generate a **fresh** code in a shell
   on the Linux host, as the same OS user that owns the managed daemon:

   ```sh
   codex remote-control pair
   ```

6. Enter that code in the Windows pairing prompt. A fresh code was needed for
   this additional client; no fixed code lifetime or general reuse policy was
   established by this test. Do not re-enable or restart the already-connected
   daemon just to generate another code.
7. Confirm the device shows **Connected**. Return to the **Codex** panel and
   open the intended existing project/conversation. The human reported that
   the existing sessions appeared there.

The screenshots show the Settings route and a Connected device row. The
browser authorization and code-entry screens were not captured, so this guide
intentionally does not invent their exact button labels or URLs. Full access
was incidentally visible elsewhere in Settings; enabling it is **not** a step
in this connection flow. Use the same-thread check below on either client.

## 5. Verify the same agent and graph

Pairing alone is not the acceptance test:

1. On the connected iPhone or Windows client, open the intended conversation
   and send a harmless, unique phrase.
2. Confirm it appears in that same conversation on the terminal. Compare the
   actual thread ID when available, rather than just matching a chat title.
3. Ask the agent to call an ENGRAM tool and record that you sent the phrase.
   Require the **returned observation ID**, not a claim that it remembers.
4. From the terminal, ask it to inspect that exact observation. Confirm the
   reply and ID are visible on the remote client too.

Our test verified a phone-to-existing-thread message, a successful local
ENGRAM write, and a reply received on the phone. The terminal inspection step
above is an additional recommended cross-device check. Record your versions
and results before claiming the complete checklist passed.

## 6. Make it available after normal auto-start

On CLI 0.164.0, `enable-remote-control --help` explicitly describes enabling
Remote Control for **future starts** and the current managed daemon. We
verified `remoteControlEnabled: true` persisted in the default daemon settings
file, `~/.codex/app-server-daemon/settings.json`.

There are two separate requirements:

- Keep that persisted setting enabled; do **not** rerun the enable command
  inside every morning conversation, where it may interrupt the daemon.
- Have your ordinary startup mechanism start the same user's managed daemon
  and attach the terminal to it. The idempotent daemon-start command is
  `codex app-server daemon start`. Our existing morning launcher already does
  this; a new user's startup mechanism must be configured separately. This host
  startup setting does not automatically launch a Windows client or phone app.

After the next cold boot, repeat the live-status and remote-client message checks.
Cold-boot persistence was **not yet exercised** in the reported test. A saved
setting is not proof that the computer booted, the daemon started, or the
network connection succeeded. No forced restart is needed just to check it.

## Troubleshooting and security

- **Hook prompt blocks the agent:** have the human review and approve hooks
  locally. Do not disable the trust gate.
- **Client shows old chats or a stale host:** confirm the account/workspace,
  fresh host connection, OS user, managed socket, and exact thread. Desktop
  and CLI configuration sharing does not merge different OS users' graphs.
- **No manual-pairing option:** record the app version and visible UI. This
  path may not be available to that rollout/account. Consult the official
  desktop flow; do not invent a button or expose an app-server port.
- **Enable waits or live status stays disabled:** preserve the error and
  arrange an idle/reconnect window. Do not issue repeated enable commands,
  force a daemon restart, or fall back to `--no-daemon` during an active chat.
- **Permissions matter:** remote access can use the host's files, tools and
  credentials within its configured permission boundary. Review that boundary
  first; do not disable approvals or sandboxing just to make pairing work.
- **Keep local transports local:** do not publish the Unix socket or bind an
  unauthenticated app-server/MCP endpoint to the LAN or internet.
- **Code expiry is not device revocation:** review connected-device controls
  when access is no longer wanted. Do not log out or revoke unrelated devices
  as a speculative fix. Per the
  [official troubleshooting guide](https://learn.chatgpt.com/docs/remote-connections),
  signing out of ChatGPT turns Remote Control off but preserves device pairings.
  After signing back in, re-enable it once at an idle moment, following section
  2. This is recovery after sign-out, not an enable command to run every boot.
  We did not exercise a sign-out/sign-in cycle in this test.
- **Local tools do not imply offline inference:** check account data controls
  before sending unpublished research or other sensitive material.

## Agent handoff checklist

- [ ] Local ENGRAM works; hooks approved by the human.
- [ ] Terminal and remote client target one intended managed daemon and thread.
- [ ] Human authorized remote access; live status is connected.
- [ ] Pairing code handled privately; remote-client steps match the actual app.
- [ ] Same-thread message, ENGRAM node creation/readback, and remote-client reply checked.
- [ ] Persisted setting and startup mechanism checked separately.
- [ ] Next cold-boot behavior verified, or explicitly marked untested.
- [ ] No other graph copied, public listener added, or approvals bypassed.
