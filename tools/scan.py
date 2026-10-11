"""Scan for nearby Bluetooth LE devices and print everything they advertise.

    .venv\\Scripts\\python tools\\scan.py [seconds]
"""
import argparse
import asyncio

from bleak import BleakScanner


async def main(seconds: float) -> None:
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = sorted(found.values(), key=lambda da: da[1].rssi, reverse=True)
    for dev, adv in rows:
        mfg = " ".join(f"{cid:04X}:{data.hex()}" for cid, data in adv.manufacturer_data.items())
        svc = " ".join(f"{u[4:8] if u.endswith('-0000-1000-8000-00805f9b34fb') else u}:{d.hex()}"
                       for u, d in adv.service_data.items())
        uuids = ",".join(u[4:8] if u.endswith("-0000-1000-8000-00805f9b34fb") else u for u in adv.service_uuids)
        print(f"{adv.rssi:>4} {dev.address}  name={adv.local_name or dev.name or ''!r}")
        if uuids:
            print(f"       uuids={uuids}")
        if mfg:
            print(f"       mfg={mfg}")
        if svc:
            print(f"       svcdata={svc}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="List nearby Bluetooth LE devices and their advertisements.")
    ap.add_argument("seconds", nargs="?", type=float, default=15.0, help="how long to scan (default 15)")
    asyncio.run(main(ap.parse_args().seconds))
