# Jarvis Home Assistant and NETGEAR setup

Jarvis uses one local Home Assistant instance for paired Google/Android TV
control and read-only NETGEAR router telemetry. Home Assistant is bound to
`127.0.0.1:8123`, so its administration page is not exposed to other LAN
devices. The container restarts automatically and is capped at two CPU cores
and 2 GB of memory.

## One-time setup

1. Run `install_home_assistant.bat` and create the local Home Assistant owner
   account in the page that opens. Keep that password private.
2. In Home Assistant, open **Settings > Devices & services > Add Integration**,
   choose **NETGEAR**, enter your router's private address, and provide its
   administrator credentials directly in Home Assistant. Never put the router
   password in Jarvis chat or the repository.
3. Turn on the NETGEAR **Traffic Meter** switch. Enable only the diagnostic
   sensors you want Jarvis to report, such as upload/download totals, link
   type, link rate, and signal strength.
4. In the Home Assistant profile, create a long-lived access token. Store it
   only in the ignored local `.env` file:

   ```dotenv
   JARVIS_HOME_ASSISTANT_NETWORK_ACCESS=netgear-readonly
   JARVIS_HOME_ASSISTANT_URL=http://127.0.0.1:8123
   JARVIS_HOME_ASSISTANT_TOKEN=replace-locally
   ```

   Loopback HTTP is accepted because the token never leaves this computer. If
   Home Assistant runs on another LAN device, configure HTTPS and use an exact
   private IP origin. The certificate must be valid for that IP and its issuing
   CA must be trusted by Windows; Jarvis does not bypass certificate or hostname
   validation. A local TLS reverse proxy is usually easier than a self-signed
   certificate. Jarvis refuses to send its bearer token over plaintext LAN.

5. For Google/Android TV control, add the **Android TV Remote** integration,
   enter the pairing code shown on the TV, and add its exact `remote.*` entity:

   ```dotenv
   JARVIS_HOME_ASSISTANT_ACCESS=paired
   JARVIS_HOME_ASSISTANT_ENTITIES=remote.example_tv
   ```

6. Restart Jarvis. Ask: `Scan my home network and explain every device and its
   router telemetry.`

## Accuracy and safety boundaries

- Router-sourced device trackers are read-only. GPS/person trackers are ignored.
- Device type is a confidence-scored inference from router names and advertised
  metadata. Jarvis labels unknown devices as unknown instead of guessing.
- Link rate is negotiated Wi-Fi/Ethernet capacity, not actual usage.
- The configured NETGEAR router provides router-wide Traffic Meter totals, not authoritative
  per-device byte totals.
- Jarvis never returns the Home Assistant token or router administrator password.
- Device-changing actions remain limited to exact allowlisted `remote.*`
  entities and require approval for the exact action.
- A successful Home Assistant service response means only that the service call
  was accepted. A completed entity readback means only that Jarvis could read
  the device state after the call; neither claim proves a physical effect.
- App launch is effect-verified only when the entity's `current_activity`
  exactly matches the requested package. Remote navigation, media, volume,
  mute, and power effects are not observable from the generic entity state, so
  Jarvis reports their effect verification as unknown rather than successful.

Official references:

- https://www.home-assistant.io/integrations/netgear
- https://www.home-assistant.io/integrations/androidtv_remote/
- https://developers.home-assistant.io/docs/api/rest/
