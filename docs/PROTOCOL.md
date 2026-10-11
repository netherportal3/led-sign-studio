# iLedColor Bluetooth protocol

What LED Sign Studio sends to the sign, and what was checked on real hardware (a 64x16 full color
sign). `iledsign.py` implements all of it. The protocol was first worked out by
[winnerplace/ha-iledcolor-led-matrix](https://github.com/winnerplace/ha-iledcolor-led-matrix) and
[zagi/iledcolor_small](https://github.com/zagi/iledcolor_small).

All numbers are big-endian unless noted.

## Finding the sign

- It advertises as `iledcolor-XXXX`, where `XXXX` is the end of its Bluetooth address. Its idle
  screen shows `L:XXXX` when it has nothing valid to show.
- No pairing. USB is power only: everything goes over Bluetooth LE.
- The manufacturer data holds a capability blob that starts with the bytes `54 42 44` ("TBD").
  Count the 2-byte company id (little-endian) as part of the data, since bleak splits it off.
  From the start of the blob:

  | Offset | Size | Field |
  |---|---|---|
  | 4 | 2 | height |
  | 6 | 2 | width |
  | 8 | 1 | color type (3 = full RGB) |
  | 14 | 2 | feature bits (bit 0 = built-in clock; clear on the tested sign) |

## GATT

Service `0xA950`:

| Characteristic | Use |
|---|---|
| `0xA951` | commands (write without response) |
| `0xA952` | upload data (write without response) |
| `0xA953` | acknowledgements (notify) |

## Frames

Every command, chunk and ack has the same envelope:

```
54 <op> <length:2> <payload> <checksum:2>
```

`length` is the payload length plus 2. `checksum` is the sum of all bytes before it, modulo 65536.
Acks come back on `0xA953` with the same `op`. The header and end acks carry a status byte at
offset 4.

| Op | Where | Payload |
|---|---|---|
| `0x0D` connect | A951 | `00` |
| `0x0F` password check | A951 | 6 bytes, all zero = no password |
| `0x06` program header | A951 | CRC (4) + total length (4) + `00 00 00` |
| `0x00` chunk | A952 | index (4) + chunk length (2) + chunk bytes |
| `0x01` end | A952 | `01` |
| `0x09` brightness | A951 | `10 - level` + 8 zero bytes (level 1 = dim to 10 = bright) |
| `0x0A` display on/off | A951 | `01` or `00` + 8 zero bytes |

## Connecting

1. Connect and subscribe to `0xA953`.
2. Send connect (`0x0D`) and wait for its ack.
3. Send the password check (`0x0F`) with six zero bytes and wait for its ack.

## Uploading a program

Each upload replaces the whole stored program. The sign keeps showing the old one until the new
upload ends, then switches at once.

1. Build the program envelope:

   ```
   CRC-32C(4) | 01 | 19 zero bytes | parameters | body
   ```

   The CRC covers everything after itself. It is CRC-32C (Castagnoli): reflected polynomial
   `0x82F63B78`, initial value `0xFFFFFFFF`, final XOR `0xFFFFFFFF`.
2. Send the header (`0x06`) with the envelope's first 4 bytes and its total length. The ack status
   is `2` when the sign's storage is full and `3` when it already shows this exact program (the
   upload can stop there).
3. Send the envelope in chunks (`0x00`), numbered from 0. The chunk size is the MTU minus 32,
   between 20 and 480 bytes. Each chunk is acked.
4. Send the end marker (`0x01`). Its ack has status `1` when the program was accepted.

### Still pictures and slideshows (raw pixels)

Parameters, 22 bytes:

```
4 zero | width(2) | height(2) | 3 zero | source type 0 | frame count(2)
| effect | speed | dwell | 0 | brightness (100) | 3 zero
```

The body is the frames as RGB bytes, row by row (`width * height * 3` each). With more than one
frame, a 2-byte speed follows them, and the sign plays the frames as a slideshow with its own
transition. Effects: `0` static, `1` left, `2` right, `3` up, `4` down, `5` snow, `6` roll,
`7` laser. The units of speed and dwell are not known. The app keeps speed at 1 or more and dwell
at 5 or more (30 or more for a single still frame).

### Animations (GIF)

Parameters, 11 two-byte fields: `0, 0, width, height, 0, 6, 1, 100, 0x400, 100, 0`. Source type 6
means the body is a GIF file. The sign decodes it and loops it by itself, with the timing in the
GIF, also after the PC disconnects. The app writes one shared palette and no local color tables.

## Verified on real hardware

- 64x16 pixels, full RGB, plain R, G, B byte order. The size is read from the advertisement.
- One connection at a time. Close the phone app first.
- A still picture uploads in about 0.7 to 1.2 seconds. Larger uploads run at about 6.5 KB per
  second. An 81 KB GIF with 446 frames worked.
- GIF playback tops out near 25 frames per second. Frames shorter than about 40 ms do not play
  faster: 20 ms stutters, and 30 ms looks the same as 40 ms. The app never goes below 30 ms.
- A program wider than 64 pixels is rejected: the sign falls back to its idle screen. Never send
  one.
- The sign has no built-in clock, so a clock has to be drawn into the uploaded GIF.
- Brightness (`0x09`) and display on/off (`0x0A`) are acknowledged and work.
- The sign keeps its program and brightness when it is unplugged and plugged back in.
- When a program is killed without disconnecting, Windows holds the Bluetooth link for about
  14 seconds, and the sign cannot be found until it lets go. Disconnect before exiting.
- Every upload is probably written to the sign's flash storage, so do not refresh it every second
  for days.
