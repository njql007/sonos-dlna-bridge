# Sonos DLNA Bridge

An experimental, local-network UPnP/DLNA MediaRenderer bridge that lets a compatible music app cast to a Sonos speaker using its own casting interface. Built to use NetEase Cloud Music (网易云音乐) with a Sonos Play:3, without AirPlay.

```text
Music app ── DLNA discovery and playback commands ──> Python bridge ──> Sonos
Audio URL ─────────────────────────────────────────> HTTP relay ────> Sonos
```

The bridge runs on an always-awake computer, advertises a virtual renderer, forwards playback commands to Sonos, and relays audio URLs supplied by the app. It uses only the Python standard library.

## Requirements

- Python 3.9 or later.
- A Sonos speaker exposing its local UPnP services, and its IP address or hostname.
- A music app with a DLNA casting option.
- Phone, bridge computer, and speaker on the same LAN, with multicast discovery allowed.
- The bridge computer must stay awake and connected throughout playback.

macOS is the tested host. The standalone Python service can also be tried on Linux; the included background-service manager is macOS-only. Other Sonos models and host platforms have not been verified.

## Quick start

```sh
git clone https://github.com/njql007/sonos-dlna-bridge.git
cd sonos-dlna-bridge
python3 bridge.py --target YOUR_SONOS_IP --name 'Sonos DLNA'
```

Replace `YOUR_SONOS_IP` with your speaker's LAN address or hostname. Allow local-network access when prompted. In the music app's own casting/device picker, select **Sonos DLNA**.

The service uses TCP port `17863` and SSDP multicast on UDP port `1900`. Its startup message prints its LAN URL; append `/status` to inspect counters, subscription count, and the last action/error. Do not expose it through router port forwarding.

Optional arguments:

```sh
python3 bridge.py --help
# Optional persistent media mappings and rotating logs:
python3 bridge.py --target YOUR_SONOS_IP --state media-state.json --log bridge.log
```

`--bind` selects the local IPv4 address; `--port` changes the HTTP port. By default, the local address is selected from the route to the speaker.

## Run at login on macOS

```sh
python3 manage.py install --target YOUR_SONOS_IP --name 'Sonos DLNA'
python3 manage.py status
python3 manage.py stop
python3 manage.py start
```

Installation copies `bridge.py` to `~/Library/Application Support/SonosDLNABridge/` and registers `~/Library/LaunchAgents/local.sonos.dlna-bridge.plist`. It runs as the logged-in user, using the Python interpreter that ran the installer. Keep that interpreter available. No administrator access is needed.

To apply source updates, repeat the install command with your target and desired name. This restarts the bridge and clears active event subscriptions. Fully close and reopen the music app, then select the renderer again if its controls become out of sync.

```sh
python3 manage.py uninstall
```

Uninstall stops the service and removes its login registration; installed files, logs, and media mappings remain for manual removal or recovery.

## Compatibility and known limitations

- **Confirmed on one iOS NetEase Cloud Music + Sonos Play:3 setup:** discovery, audible playback, pause, resume, and seeking while playing. Pause/resume became reliable in the user's latest test after fully closing and reopening the app. This is not a guarantee across app versions or longer sessions.
- **Android:** the protocol is platform-independent, but Android has not yet been tested.
- **Seeking while paused:** in the observed iOS session, the app did not send a Seek command while paused, so resume continued at the old position. Workaround: resume first, then drag the progress slider.
- App state can become stale after a bridge restart. Disconnect/reconnect alone was insufficient in the tested session; fully reopening the app restored state notifications.
- Automatic track progression and broad format/model compatibility are not yet verified.
- No transcoding, account login, or protected-content decryption. Playback depends on the app supplying an accessible HTTP(S) audio URL and Sonos supporting the format.
- Duration recovery supports MP4 `mvhd` and FLAC STREAMINFO in the first 256 KiB. Other formats/layouts retain the speaker's reported duration.
- AirConnect can coexist as a separate AirPlay path, but controlling the same speaker through both paths can interrupt playback. AirConnect is not required or bundled.

## Implementation

- SSDP discovery and periodic announcements for a virtual MediaRenderer.
- Device/service descriptions derived from the target Sonos, with an allowlist of playback and volume actions.
- SOAP forwarding with backend errors preserved, serialized transport controls, and state readbacks ordered after pending operations.
- HTTP audio relay with HEAD, Range, redirect handling, and original-URI restoration in client responses.
- GENA subscriptions for AVTransport, RenderingControl, and ConnectionManager. Transport notifications reflect stable Sonos states and discard outdated snapshots.
- Network address changes cause the process to exit for rediscovery; the macOS LaunchAgent restarts it automatically. Foreground runs must be restarted manually.

## Network and data handling

This is a trusted-LAN service without user authentication. Other clients on that LAN can control the renderer. Event callbacks are restricted to the subscribing client's address. Media destinations and redirects are validated to restrict unrelated private-network access. A narrow exception allows `*.music.126.net` hosts resolving into `198.18.0.0/15`, for proxy DNS setups using synthetic addresses.

Logs omit full audio URLs and song metadata but contain local addresses and control activity. Optional `media-state.json` stores up to 128 recent relay mappings and may contain signed audio URLs; it is written with owner-only permissions. Never publish that file or runtime logs. They are excluded by `.gitignore`.

## Tests

```sh
python3 test_bridge.py
```

The automated suite uses local fake endpoints and does not control a real speaker. It covers SOAP forwarding, errors, media relaying, HEAD/Range, redirects, address restrictions, subscriptions, duration parsing, and transport/event ordering.

Separate manual checks on the tested setup verified live discovery, service descriptions, state/volume/protocol queries, event subscription lifecycle, and a play/pause/resume cycle. App-level behavior still requires real-device testing.

## Acknowledgments

The DLNA relay approach was informed by [XiaoaiCast-Pro](https://github.com/dawuZZZ/XiaoaiCast-Pro). This is an independently written Python implementation using the services exposed by Sonos; it does not depend on that Android project.

[AirConnect](https://github.com/philippe44/AirConnect) provides a separate AirPlay bridge and was useful as an alternative playback path. This project is not affiliated with Sonos or NetEase.
