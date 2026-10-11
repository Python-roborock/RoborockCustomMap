# Roborock Custom Map

you MUST be on 2025.12.1 or later

This allows you to use the core Roborock integration with the [Xiaomi Map Card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card)

If you would like to support me, you can do so here:

[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

[![PaypalMe][paypalmebadge]][paypalme]

### Setup

1. Install the [Roborock Core Integration](https://my.home-assistant.io/redirect/config_flow_start?domain=roborock) and set it up
2. It is recommended that you first disable the Image entities within the core integration. Open each image entity, hit the gear icon, then trigger the toggle by enabled.
3. Install this integration(See the installing via HACS section below)
4. This integration works by piggybacking off of the Core integration, so the Core integration will do all the data updating to help prevent rate-limits. But that means that the core integration must be setup and loaded first. If you run into any issues, make sure the Roborock integration is loaded first, and then reload this one.
5. Setup the map card like normal! An example configuration would look like
```yaml
type: custom:xiaomi-vacuum-map-card
vacuum_platform: Roborock
entity: vacuum.s7
map_source:
  camera: image.s7_downstairs_full_custom
calibration_source:
  camera: true
```

### Custom Floor Plan

You can now override a Stock Floor Plan with your own Custom Floor Plan
(for example a tidied-up or stylized version).

#### How?
1. `Settings` → `Devices & Services` → `Roborock Custom Map` → press `Configure`.
2. Pick a Floor (only if > 1 available).
3. Choose your Floor Plan (PNG, JPEG or WebP); a Live Preview appears as you do so.
4. Press `Submit` — the Floor Plan applies immediately as-is, with no adjustments.
5. On the next view, optionally adjust the horizontal/vertical offset/scale
   of the Custom Floor Plan, or the relative rotation of the Physical Walls.
   The Physical Walls are imposed over the Custom Floor Plan for your convenience.
   The Live Preview refreshes in near-real-time.
6. Press `Submit` once more — to keep the adjustments,
   or simply press `X` — to keep the Floor Plan as-is.

Reopen `Configure` **at any time** to Adjust, Replace, or Remove the Custom Floor Plan.

### Installation

### Installing via HACS
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Lash-L&repository=RoborockCustomMap&category=integration)

or

1. Go to HACS->Integrations
1. Add this repo(https://github.com/Lash-L/RoborockCustomMap) into your HACS custom repositories
1. Search for Roborock Custom Map and Download it
1. Restart your HomeAssistant
1. Go to Settings->Devices & Services
1. Add the Roborock Custom Map integration

### Alternative/optional

Once you set up this integration, you can generate a static config in the lovelace card, and theoretically, you should be able to use that code with your Roborock CORE integration. However, it wont stay up to date if the map calibrations change significantly, or rooms change. So I'd only do this when I was sure everything was good!



[buymecoffee]: https://www.buymeacoffee.com/LashL
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[paypalme]: https://paypal.me/LLashley304
[paypalmebadge]: https://cdn.rawgit.com/twolfson/paypal-github-button/1.0.0/dist/button.svg
[hacsbutton]: https://my.home-assistant.io/redirect/hacs_repository/?owner=Lash-L&repository=tempofit&category=integration
