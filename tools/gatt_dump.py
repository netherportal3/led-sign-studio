"""Connect to a sign and list every GATT service and characteristic. Read-only.

    .venv\\Scripts\\python tools\\gatt_dump.py               the first iledcolor-XXXX sign found
    .venv\\Scripts\\python tools\\gatt_dump.py AA:BB:CC:DD:EE:FF

Close LED Sign Studio first: the sign accepts one connection at a time.
"""
import argparse
import asyncio
import os
import sys

from bleak import BleakClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import iledsign  # noqa: E402


async def main(address: str | None) -> None:
    if not address:
        found = await iledsign.find_sign()
        print(f"found {found.name} {found.address} {found.width}x{found.height}")
        address = found.address
    async with BleakClient(address, timeout=20) as client:
        print(f"connected={client.is_connected} mtu={client.mtu_size}")
        for svc in client.services:
            print(f"[service] {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                line = f"   [char] {ch.uuid} handle={ch.handle} ({props}) {ch.description}"
                if "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch)
                        line += f"\n          value={val.hex()} {val!r}"
                    except Exception as e:  # noqa: BLE001
                        line += f"\n          read failed: {e}"
                print(line)
                for d in ch.descriptors:
                    print(f"      [desc] {d.uuid} handle={d.handle}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="List the GATT services and characteristics of a sign (read-only).")
    ap.add_argument("address", nargs="?", help="Bluetooth address (default: the first iledcolor sign found)")
    try:
        asyncio.run(main(ap.parse_args().address))
    except iledsign.SignNotFound as e:
        raise SystemExit(str(e)) from None
