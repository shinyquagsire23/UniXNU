#!/usr/bin/env python3
"""Pack the owner-built Adreno Metal plugin into a GPU addon file for the boot payload.

  make-gpu-addon.py [--publish] [--selftest] [--ca-prefs] [--clients NAME,...] [--wallpaper DIR] [--prime-list FILE] OUTPUT

The files are the ones add-gpu-plugin-data.py stages into a Data seed, with the same paths below /private/var/ngpu and the same modes:
addons/NativeAdrenoMetal.bundle/*, addons/NativeAdrenoMetal.publish (--publish), addons/NativeAdrenoMetal.selftest (--selftest: the plugin's once-per-process
on-device self-test, NativeAdrenoMetalPlugin.mm), addons/NativeAdrenoMetal.clients (--clients),
prefs-mobile/ and prefs-root/ {com.apple.coreanimation,com.apple.backboardd,backboardd}.plist (--ca-prefs: the render server's Core Animation preferences
add-gpu-plugin-data.py writes into a seed, no deep framebuffer and no compression; the kernel writes the two prefixes to
/private/var/{mobile,root}/Library/Preferences),
bin/adrenoprobed and daemons/*.plist. --wallpaper adds the files of DIR (scripts/make-wallpaper-overlay.py) as wallpaper/*, which the
kernel mounts over the default wallpaper's layered animation. --prime-list adds FILE (the absolute paths of the OS image's .metallib
files, one per line) as addons/NativeAdrenoMetal.primelist, the libraries the render server compiles ahead (PrimeShaders.h). The payload carries the file next to the kernel (unixnu build payload --gpu-addon), the loader
hands it to the kernel as /chosen/memory-map/GpuAddon, and the kernel writes every file whose content or mode differs into the mounted
Data volume before its addon mounts (sheng-gpu-addon=1). The Data seed then no longer changes with the plugin, so its persistent store
(whose identity is the seed's content) survives a plugin rebuild. The kernel creates missing directories.

The data of every file is zlib-compressed: the payload FAT (the boot image's ramdisk) has to stay at 192 MiB, which the tablet's boot
loader accepts (a 256 MiB ramdisk does not start the firmware), and the kernel and the Data seed already take most of it.

Layout, little endian: "NGPUADD2", u32 count, u32 0, u64 total bytes; per file u32 path bytes (with its NUL, at most 160), u32 mode,
u64 size, u64 compressed size, the path padded to 8 bytes, the zlib stream padded to 8 bytes."""
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'build/adreno-metal-plugin'
BUNDLE = PLUGIN / 'NativeAdrenoMetal.bundle'


def pad(data):
    return data + bytes(-len(data) % 8)


def main():
    args = [a for a in sys.argv[1:] if a not in ('--publish', '--selftest', '--ca-prefs')]
    ca_prefs = '--ca-prefs' in sys.argv[1:]
    publish = '--publish' in sys.argv[1:]
    selftest = '--selftest' in sys.argv[1:]
    clients = None
    wallpaper = None
    if '--wallpaper' in args:
        at = args.index('--wallpaper')
        wallpaper = Path(args[at + 1])
        del args[at:at + 2]
        if not wallpaper.is_dir():
            sys.exit('--wallpaper takes a directory')
    prime_list = None
    if '--prime-list' in args:
        at = args.index('--prime-list')
        prime_list = Path(args[at + 1])
        del args[at:at + 2]
        lines = prime_list.read_text().split()
        if not lines or any(not line.startswith('/') or not line.endswith('.metallib') for line in lines):
            sys.exit('--prime-list takes a file of absolute .metallib paths')
    if '--clients' in args:
        at = args.index('--clients')
        clients = [name for name in args[at + 1].split(',') if name]
        del args[at:at + 2]
        if not clients or any(not all(c.isalnum() or c in '._-*' for c in name) for name in clients):
            sys.exit('--clients takes process names separated by commas')
    if len(args) != 1:
        sys.exit(__doc__)
    output = Path(args[0]).resolve()
    if output.exists():
        sys.exit('Refuse to overwrite ' + str(output))
    if not (BUNDLE / 'NativeAdrenoMetal').is_file():
        sys.exit('Build the plugin first: python3 scripts/build-adreno-metal-plugin.py')
    files = []
    for path in sorted(p for p in BUNDLE.rglob('*') if p.is_file()):
        mode = 0o755 if path.name in ('NativeAdrenoMetal', 'libvulkan_freedreno.dylib') else 0o644
        files.append(('addons/NativeAdrenoMetal.bundle/' + path.relative_to(BUNDLE).as_posix(), mode, path.read_bytes()))
    if publish:
        files.append(('addons/NativeAdrenoMetal.publish', 0o644, b'1\n'))
    if selftest:
        files.append(('addons/NativeAdrenoMetal.selftest', 0o644, b'1\n'))
    if ca_prefs:
        # backboardd picks a 10-bit deep framebuffer unless told not to and reads this when it starts, before the plugin can act
        import plistlib
        values = plistlib.dumps({'CAEnableDeepFramebuffer': False, 'CAEnableFramebufferCompression': False,
                                 'CAEnableIOSurfaceCompression': False}, fmt=plistlib.FMT_BINARY)
        # Every domain the keys can be read from without touching .GlobalPreferences (which the kernel cannot merge): QuartzCore's own
        # (com.apple.coreanimation), the render server's bundle identifier and its process name. pdx203 usb-30: backboardd.plist alone
        # left the deep framebuffer on (every refused surface 'b3a8', 30RGBLE_8A_BiPlanar).
        for domain in ('com.apple.coreanimation', 'com.apple.backboardd', 'backboardd'):
            files.append(('prefs-mobile/%s.plist' % domain, 0o644, values))
            files.append(('prefs-root/%s.plist' % domain, 0o644, values))
    if clients:
        files.append(('addons/NativeAdrenoMetal.clients', 0o644, ('\n'.join(clients) + '\n').encode()))
    if prime_list:
        files.append(('addons/NativeAdrenoMetal.primelist', 0o644, prime_list.read_bytes()))
    files.append(('bin/adrenoprobed', 0o755, (PLUGIN / 'adrenoprobed').read_bytes()))
    for plist in sorted(PLUGIN.glob('*.plist')):
        if not plist.name.endswith('-entitlements.plist'):   # build input, not a launchd job
            files.append(('daemons/' + plist.name, 0o644, plist.read_bytes()))
    if wallpaper:
        for path in sorted(p for p in wallpaper.rglob('*') if p.is_file()):
            files.append(('wallpaper/' + path.relative_to(wallpaper).as_posix(), 0o644, path.read_bytes()))
    body = b''
    for name, mode, data in files:
        encoded = name.encode() + b'\0'
        if len(encoded) > 160 or '..' in name.split('/') or name.startswith('/'):
            sys.exit('Bad addon path ' + name)
        packed = zlib.compress(data, 9)
        body += struct.pack('<IIQQ', len(encoded), mode, len(data), len(packed)) + pad(encoded) + pad(packed)
    blob = b'NGPUADD2' + struct.pack('<IIQ', len(files), 0, 24 + len(body)) + body
    output.write_bytes(blob)
    for name, mode, data in files:
        print(f'{mode:o} {len(data):>10} {name}')
    print(f'{output}: {len(files)} files, {len(blob)} bytes')


if __name__ == '__main__':
    main()
