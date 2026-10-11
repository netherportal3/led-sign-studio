# Third-party notices

## iledcolor_small

Parts of `iledsign.py` are adapted from [zagi/iledcolor_small](https://github.com/zagi/iledcolor_small):
the command frame envelope, the handshake, the upload stream (start, chunks, end), the CRC-32C
routine and the GIF program fields. Used under the MIT License:

```
MIT License

Copyright (c) 2026 Michal Zagalski

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## ha-iledcolor-led-matrix

The raw-pixel program parameters, the capability blob in the advertisement and the transition
effect ids were learned from
[winnerplace/ha-iledcolor-led-matrix](https://github.com/winnerplace/ha-iledcolor-led-matrix).
That repository has no license file, so no code is taken from it: `iledsign.py` builds and parses
those byte layouts with its own code.
