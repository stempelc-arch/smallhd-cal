#!/usr/bin/env python3
from __future__ import annotations

import argparse
import struct
from pathlib import Path

ENTRY_SIZE = 36
HEADER_SIZE = 20
MAGIC = 0xB5D3C395


def parse(path: Path):
    data = path.read_bytes()
    magic, version, count, total_size, _reserved = struct.unpack_from('<IIIII', data, 0)
    if magic != MAGIC:
        raise ValueError(f'Unexpected magic {magic:#x}, expected {MAGIC:#x}')
    print(f'Magic:      {magic:#x}')
    print(f'Version:    {version}')
    print(f'Entries:    {count}')
    print(f'Total size: {total_size} bytes')
    print()
    entries = []
    for i in range(count):
        pos = HEADER_SIZE + i * ENTRY_SIZE
        raw = data[pos:pos + ENTRY_SIZE]
        name = raw[:16].split(b'\0', 1)[0].decode('ascii', errors='replace')
        load_addr, offset, size, crc, extra = struct.unpack_from('<IIIII', raw, 16)
        entries.append((i, name, load_addr, offset, size, crc, extra))
        print(f'{i:02d} {name:16s} load={load_addr:#010x} off={offset:#010x} size={size:#010x} crc={crc:#010x} extra={extra:#x}')
    return data, entries


def extract(data: bytes, entries, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    for i, name, load_addr, offset, size, crc, extra in entries:
        if size == 0 or offset + size > len(data):
            continue
        safe = f'{i:02d}_{name or "noname"}_{offset:x}_{size:x}.bin'.replace('/', '_')
        (outdir / safe).write_bytes(data[offset:offset + size])
    print(f'\nExtracted payloads to: {outdir}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('firmware', type=Path)
    ap.add_argument('--extract', type=Path, default=None)
    args = ap.parse_args()
    data, entries = parse(args.firmware)
    if args.extract:
        extract(data, entries, args.extract)

if __name__ == '__main__':
    main()
