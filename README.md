# RBXMonkey - Blender Animations Plugin for Roblox

A powerful Roblox Studio plugin that enables seamless animation workflow between Blender and Roblox, featuring real-time sync, advanced rigging tools, and comprehensive animation management.

## Installation

1. Download the latest release from [Blender Extensions](https://extensions.blender.org/approval-queue/roblox-animations-importer-exporter/) or [Releases](https://github.com/cautioned/blender-animations-plugin/releases) page
2. Install the plugin on Roblox Studio: [Get Here](https://create.roblox.com/store/asset/16708835782/Blender-Animations-ultimate-edition)
3. Set up the Blender addon (see Blender Setup below)

## Blender Setup

1. Install the Blender addon in blender's preferences (download zip) [Blender Extensions](https://extensions.blender.org/approval-queue/roblox-animations-importer-exporter/) or [Releases](https://github.com/cautioned/blender-animations-plugin/releases) page
2. Install the Roblox addon: https://create.roblox.com/store/asset/16708835782/
2. Enable the addon in Blender's preferences
3. The addon is found in the 3D view toolbar press 'N' then click the 'RBXMonkey' tab
4. Start the Blender server on port 31337 (default), or your port of choice. 
5. Connect to the server using the Blender Sync tab on the Roblox Plugin.

## Usage

### Basic Workflow

1. **Connect to Blender**: Use the "Blender Sync" tab
2. **Select a Rig**: Choose your armature in the Blender Sync tab
3. **Import Animation**: Click "Import from Blender" to bring animations into Roblox
4. **Play & Edit**: Use the playback controls to preview and edit animations
5. **Export Back**: Send animations back to Blender for further editing

### Features

- **Live Sync**: Enable live sync for real-time updates between Blender and Roblox
- **Bone Sync**: Create bones in Blender and sync them in studio as motor6ds.
- **Bone Toggles**: Use the Tools tab to enable/disable specific bones
- **Animation Scaling**: Adjust scale factors in the Tools tab
- **Camera Controls**: Attach a camera to a part, such as the head. Useful for viewport animations.
- **Easing Transfer**: Easing transfers losslessly between Roblox and Blender where possible.

## Configuration

### Plugin Settings
- **Auto-connect**: Automatically connect to Blender on startup
- **Live Sync**: Enable real-time synchronization
- **File Export**: Allow exporting animations to files

## Roblox asset property compatibility

The binary `.rbxm`/`.rbxl` importer accepts both legacy asset ID properties
and the new `Content` properties used by Studio saves starting November 11,
2026. This includes meshes, textures, PBR maps, clothing, wrap metadata,
beams, and skyboxes. Clothing assets fetched as XML support both legacy
`<url>` values and new `<uri>`/`<null />` values.

When both property names exist, the new Content value takes precedence,
including an explicitly empty value. Object-backed Content has no asset URI
and is treated as empty; importing embedded EditableMesh/EditableImage data
is not supported. Full XML place/model import is not supported.

Development tooling requires Rojo **7.7.1 or later**; `aftman.toml` pins 7.7.1.
Existing Studio Luau property reads and writes remain compatible. See the
[Roblox migration announcement](https://devforum.roblox.com/t/upcoming-changes-to-asset-properties-content-property-serialization-switch/4975546).

## Visual matching tests

The importer includes measured EEVEE lighting improvements for Realistic scenes
without an imported sky image, consistent sRGB color conversion, and support
for SurfaceAppearance tint/emission and attachment-based lights. This is an
approximation of Studio's renderer; indoor fill, reflections, atmosphere, and
post-processing still need further calibration.

See [the visual comparison suite](tools/visual_parity/README.md) to generate
Studio reference scenes, capture images through CaptureService, render matching
EEVEE views, and inspect image differences.

## Troubleshooting

### Common Issues

**Connection Failed**
- Ensure Blender server is running on the correct port
- Roblox plugin must have network permissions enabled.
- Check firewall settings
- Verify the Blender addon is properly installed


## Contributing

1. Test for stability and submit a pull request.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

**Important**: This software is forever free and open source. Commercial use is permitted under the GPL-3.0 license, but any derivative works must also be open source and free. This prevents proprietary monetization while allowing donations and community contributions. DO NOT reupload this to the Roblox Creator Store as a paid plugin (free with donations is allowed however). If you disagree with this practice, you may program your own version based off the original addon by Den_S https://devforum.roblox.com/t/blender-rig-exporteranimation-importer/34729, which has no such copyleft restrictions.

## Support

- **Issues**: Report bugs and request features on [GitHub Issues](https://github.com/cautioned/blender-animations-plugin/issues)
- **Discord**: Join the discord server for help: [JOIN HERE](https://discord.gg/qNB3EhxYYp)

---

**Made with ❤️ for the Roblox and Blender communities**

Material round-trip comparisons: see [the material test workflow](tools/material_parity/README.md) for the 240-sample Studio → native RBXM → EEVEE suite and its limits.

Neon glow comparisons: see [the Neon test workflow](tools/neon_parity/README.md) for the EEVEE viewport/render compositor and source calibration.
